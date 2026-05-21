"""
Visualization routes for wall analysis
"""

from flask_openapi3 import APIBlueprint
from flask import request, jsonify, g
import logging
import time
import os
import cv2
import numpy
from datetime import datetime

from models.mask_rcnn_model import get_model, get_config, is_model_initialized
from services.image_validation import validate_and_resize_image, check_memory_usage
from image_processing.image_loader import myImageLoader

from utils.error_handlers import ModelNotReadyError, ImageValidationError
from utils.validators import require_image_upload, validate_scale_factor
from utils.inference_executor import get_executor
from utils.geometry import safe_logical_or, safe_logical_and
from utils.conversions import (
    pixels_to_mm, pixels_sq_to_mm_sq, convert_junction_position_to_mm, save_wall_analysis)

from analysis.room_analysis import extract_room_polygons, find_host_wall_id
from utils.file_utils import getNextTestNumber
from schemas import AnalyzeFormRequest

from image_processing.mask_processing import (
    extract_wall_masks, segment_individual_walls)

from analysis.door_analysis import (
    analyzeDoorOrientation, generateArchitecturalNotes,
    categorize_door_size, assess_door_accessibility)

from analysis.wall_analysis import (
    extract_wall_parameters, find_wall_connections, analyze_junction_types,
    identify_exterior_walls, calculate_perimeter_dimensions)

from analysis.junction_analysis import find_junctions_from_bboxes

from analysis.window_analysis import (
    categorize_window_size, assess_window_glazing, generate_window_notes)

# Import new BIM analysis modules
from analysis.stair_analysis import extract_stair_footprint
from analysis.slab_analysis import extract_slab_polygon

from visualization.wall_visualization import create_wall_visualization

from ocr_detector import detect_space_names

from config.constants import IMAGES_OUTPUT_DIR

logger = logging.getLogger(__name__)

bp = APIBlueprint('visualization', __name__)


def _orientation_to_angle(orientation: dict) -> float:
    """
    Convert door orientation analysis to a Revit-compatible rotation angle (degrees).

    Revit door families face the +X direction (East) at 0 degrees.
    Mapping:
        opens_rightward ->   0  (faces East,  opens right)
        opens_upward    ->  90  (faces North, opens up)
        opens_leftward  -> 180  (faces West,  opens left)
        opens_downward  -> 270  (faces South, opens down)
    """
    swing_map = {
        "opens_rightward": 0.0,
        "opens_upward":    90.0,
        "opens_leftward":  180.0,
        "opens_downward":  270.0,
    }
    swing = orientation.get("estimated_swing", "unknown")
    return swing_map.get(swing, 0.0)

@bp.route('/analyze', methods=['POST'])
def analyze_floor_plan():
    """Create enhanced visualization showing wall centerlines, junctions, and wall parameters"""

    if not is_model_initialized():
        raise ModelNotReadyError()

    imagefile = require_image_upload("image")
    scale_factor_mm_per_pixel = validate_scale_factor(
        request.form.get("scale_factor_mm_per_pixel", 1.0)
    )

    # Parse optional building height parameters from the request.
    # These override the hardcoded defaults in buildEnhancedJson and ifc_exporter.
    import json as _json
    _bp_raw = request.form.get("building_params", "{}")
    try:
        building_params = _json.loads(_bp_raw) if isinstance(_bp_raw, str) else {}
    except (ValueError, TypeError):
        building_params = {}
    from utils.validators import validate_building_params
    building_params = validate_building_params(building_params)

    try:
        imagefile, resize_info = validate_and_resize_image(imagefile)

        if resize_info["reason"] in [
            "image_too_small",
            "resize_would_make_too_small",
            "image_too_large_resize_disabled",
        ]:
            raise ImageValidationError(
                f"Image validation failed: {resize_info['reason']}",
                details={
                    "original_size": resize_info["original_size"],
                    "min_size": 100,
                    "max_size": 2048,
                    "resize_allowed": True,
                },
            )
        
        memory_before = check_memory_usage()
        logger.debug(f"Memory before processing: {memory_before:.1f}MB")
        
        if resize_info["resized"]:
            original_scale = scale_factor_mm_per_pixel
            scale_factor_mm_per_pixel *= resize_info["resize_factor"]
            logger.info(f"Adjusted scale factor from {original_scale:.4f} to {scale_factor_mm_per_pixel:.4f} due to image resize")
        
        original_image = imagefile.copy()
        
        img_rgb_tmp2 = original_image.convert('RGB')
        gray_tmp = cv2.cvtColor(numpy.array(img_rgb_tmp2), cv2.COLOR_RGB2GRAY)
        edges_tmp = cv2.Canny(gray_tmp, 50, 150)
        avg_col = numpy.mean(numpy.sum(edges_tmp > 0, axis=0))
        avg_row = numpy.mean(numpy.sum(edges_tmp > 0, axis=1))
        is_office_plan = max(avg_col, avg_row) < 7
        image, w, h = myImageLoader(imagefile, enhance_for_office=is_office_plan)
        logger.info(f"Creating wall analysis visualization for image: {h}x{w} {'(office plan)' if is_office_plan else ''}")
        
        if resize_info["resized"]:
            logger.info(f"Image was resized: {resize_info['original_size']} -> {resize_info['new_size']}")
        
        t0 = time.time()
        model = get_model()

        # Run inference in thread pool — prevents this 15-30s call from
        # blocking the Flask worker and locking out all other users.
        r = get_executor().run(model.detect, [image], verbose=0)[0]
        logger.debug(f"Time - inference: {time.time()-t0:.2f}s")
        
        t0 = time.time()
        wall_masks, wall_indices = extract_wall_masks(r)
        logger.info(f"Extracted {len(wall_masks)} wall masks from model output")
        combined_wall_mask = numpy.zeros((h, w), dtype=bool)
        for mask in wall_masks:
            combined_wall_mask = safe_logical_or(combined_wall_mask.astype(bool), mask.astype(bool))
        
        combined_door_mask = numpy.zeros((h, w), dtype=bool)
        for idx, cid in enumerate(r['class_ids']):
            if cid == 3:
                bbox = r['rois'][idx]
                y1, x1, y2, x2 = [int(round(v)) for v in bbox]
                if 'masks' in r and idx < r['masks'].shape[2]:
                    dm = r['masks'][:, :, idx]
                    dilated_dm = cv2.dilate(dm.astype(numpy.uint8), numpy.ones((15,15), numpy.uint8), iterations=1).astype(bool)
                    dilated_dm = cv2.dilate(dilated_dm.astype(numpy.uint8), numpy.ones((35,35), numpy.uint8), iterations=1).astype(bool)
                    combined_door_mask = safe_logical_or(combined_door_mask.astype(bool), dilated_dm.astype(bool))
                margin = 40
                x1e = max(0, x1 - margin)
                y1e = max(0, y1 - margin)
                x2e = min(w-1, x2 + margin)
                y2e = min(h-1, y2 + margin)
                temp_mask = numpy.zeros_like(combined_door_mask)
                temp_mask[y1e:y2e+1, x1e:x2e+1] = True
                combined_door_mask = safe_logical_or(combined_door_mask, temp_mask)

        combined_window_mask = numpy.zeros((h, w), dtype=bool)
        for idx, cid in enumerate(r['class_ids']):
            if cid == 2:
                bbox = r['rois'][idx]
                y1, x1, y2, x2 = [int(round(v)) for v in bbox]
                if 'masks' in r and idx < r['masks'].shape[2]:
                    wm = r['masks'][:, :, idx]
                    dilated_wm = cv2.dilate(wm.astype(numpy.uint8), numpy.ones((10,10), numpy.uint8), iterations=1).astype(bool)
                    dilated_wm = cv2.dilate(dilated_wm.astype(numpy.uint8), numpy.ones((20,20), numpy.uint8), iterations=1).astype(bool)
                    combined_window_mask = safe_logical_or(combined_window_mask.astype(bool), dilated_wm.astype(bool))
                margin = 25
                x1e = max(0, x1 - margin)
                y1e = max(0, y1 - margin)
                x2e = min(w-1, x2 + margin)
                y2e = min(h-1, y2 + margin)
                temp_mask = numpy.zeros_like(combined_window_mask)
                temp_mask[y1e:y2e+1, x1e:x2e+1] = True
                combined_window_mask = safe_logical_or(combined_window_mask, temp_mask)

        combined_wall_mask = safe_logical_and(combined_wall_mask.astype(bool), numpy.logical_not(combined_door_mask.astype(bool)))
        combined_wall_mask = safe_logical_and(combined_wall_mask.astype(bool), numpy.logical_not(combined_window_mask.astype(bool)))
        logger.info("Combined wall mask ready; starting skeletonisation & segment extraction …")
        wall_segments, junctions = segment_individual_walls(combined_wall_mask)
        logger.info(f"Found {len(wall_segments)} wall segments and {len(junctions)} raw junctions")
        wall_parameters = extract_wall_parameters(wall_segments, combined_wall_mask, junctions, scale_factor_mm_per_pixel)
        logger.info(f"Computed parameters for {len(wall_parameters)} walls")
        wall_connections_viz = find_wall_connections(wall_segments, junctions)
        junction_analysis = analyze_junction_types(junctions, wall_connections_viz)
        
        for junction in junction_analysis:
            junction.update(convert_junction_position_to_mm(junction, scale_factor_mm_per_pixel))
        logger.info(f"Final junction list contains {len(junction_analysis)} junctions")
        
        exterior_walls, interior_walls = identify_exterior_walls(wall_parameters, w, h, scale_factor_mm_per_pixel)
        perimeter_dimensions = calculate_perimeter_dimensions(exterior_walls)
        logger.info(f"Identified {len(exterior_walls)} exterior walls and {len(interior_walls)} interior walls")
        logger.debug(f"Time - wall segmentation & analysis: {time.time()-t0:.2f}s")
        
        if len(junction_analysis) < 4:
            wall_bboxes = [r['rois'][idx] for idx in wall_indices]
            fallback_juncs = find_junctions_from_bboxes(wall_bboxes)
            for jx, jy in fallback_juncs:
                junction_data = {
                    "junction_id": f"J{len(junction_analysis)+1}",
                    "position": [float(jx), float(jy)],
                    "connected_walls": [],
                    "junction_type": "corner",
                    "wall_count": 2
                }
                junction_data.update(convert_junction_position_to_mm(junction_data, scale_factor_mm_per_pixel))
                junction_analysis.append(junction_data)
        
        t0 = time.time()
        door_indices = [i for i, class_id in enumerate(r['class_ids']) if class_id == 3]
        detailed_doors = []
        
        if door_indices:
            door_bboxes = [r['rois'][i] for i in door_indices]
            door_scores = [r['scores'][i] for i in door_indices]
            door_masks = r['masks'] if len(door_indices) > 0 else None
            
            for i, (bbox, confidence) in enumerate(zip(door_bboxes, door_scores)):
                door_mask_index = door_indices[i] if i < len(door_indices) else None
                door_mask = door_masks[:, :, door_mask_index] if door_masks is not None and door_mask_index is not None else None
                
                y1, x1, y2, x2 = bbox
                orientation = analyzeDoorOrientation(door_mask, bbox, w, h)

                if orientation.get("door_type") == "vertical":
                    width_px = abs(x2 - x1)
                else:
                    width_px = abs(y2 - y1)
                width_mm = pixels_to_mm(width_px, scale_factor_mm_per_pixel)

                door_width = float(x2 - x1)
                door_height = float(y2 - y1)
                door_area = door_width * door_height

                door_bbox_dict = {"x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2)}
                architectural_notes = generateArchitecturalNotes(orientation, door_bbox_dict)
                
                door_data = {
                    "door_id": i + 1,
                    "confidence": float(confidence),
                    "location": {
                        "center": {
                            "x": float(pixels_to_mm((x1 + x2) / 2, scale_factor_mm_per_pixel)),
                            "y": float(pixels_to_mm((y1 + y2) / 2, scale_factor_mm_per_pixel))
                        },
                        "relative_position": {
                            "from_left": f"{(x1/w)*100:.1f}%",
                            "from_top": f"{(y1/h)*100:.1f}%"
                        }
                    },
                    "dimensions": {
                        "width": width_mm,
                        "height": float(pixels_to_mm(door_height, scale_factor_mm_per_pixel)),
                        "area": float(pixels_sq_to_mm_sq(door_area, scale_factor_mm_per_pixel)),
                        "aspect_ratio": door_width / door_height if door_height > 0 else 0
                    },
                    "orientation": orientation,
                    "swing_angle": _orientation_to_angle(orientation),
                    "architectural_analysis": {
                        "door_type": "interior" if door_width < door_height else "entrance",
                        "size_category": categorize_door_size(door_width, door_height),
                        "accessibility": assess_door_accessibility(door_width),
                        "notes": architectural_notes
                    }
                }
                detailed_doors.append(door_data)
            
            logger.info(f"Analyzed {len(detailed_doors)} doors")
        logger.debug(f"Time - door analysis: {time.time()-t0:.2f}s")
        
        t0 = time.time()
        window_indices = [i for i, class_id in enumerate(r['class_ids']) if class_id == 2]
        detailed_windows = []
        
        if window_indices:
            window_bboxes = [r['rois'][i] for i in window_indices]
            window_scores = [r['scores'][i] for i in window_indices]
            window_masks = r['masks'] if len(window_indices) > 0 else None
            
            for i, (bbox, confidence) in enumerate(zip(window_bboxes, window_scores)):
                window_mask_index = window_indices[i] if i < len(window_indices) else None
                window_mask = window_masks[:, :, window_mask_index] if window_masks is not None and window_mask_index is not None else None
                
                y1, x1, y2, x2 = bbox
                window_width = float(x2 - x1)
                window_height = float(y2 - y1)
                window_area = window_width * window_height
                
                if window_width > window_height:
                    window_type = "horizontal"
                    width_px = window_width
                else:
                    window_type = "vertical"
                    width_px = window_height
                width_mm = pixels_to_mm(width_px, scale_factor_mm_per_pixel)
                
                window_data = {
                    "window_id": i + 1,
                    "confidence": float(confidence),
                    "location": {
                        "center": {
                            "x": float(pixels_to_mm((x1 + x2) / 2, scale_factor_mm_per_pixel)),
                            "y": float(pixels_to_mm((y1 + y2) / 2, scale_factor_mm_per_pixel))
                        },
                        "relative_position": {
                            "from_left": f"{(x1/w)*100:.1f}%",
                            "from_top": f"{(y1/h)*100:.1f}%"
                        }
                    },
                    "dimensions": {
                        "width": width_mm,
                        "height": float(pixels_to_mm(window_height, scale_factor_mm_per_pixel)),
                        "area": float(pixels_sq_to_mm_sq(window_area, scale_factor_mm_per_pixel)),
                        "aspect_ratio": window_width / window_height if window_height > 0 else 0
                    },
                    "window_type": window_type,
                    "architectural_analysis": {
                        "size_category": categorize_window_size(window_width, window_height),
                        "glazing_type": assess_window_glazing(window_width, window_height),
                        "notes": generate_window_notes(window_width, window_height, window_type)
                    }
                }
                detailed_windows.append(window_data)
            
            logger.info(f"Analyzed {len(detailed_windows)} windows")
        logger.debug(f"Time - window analysis: {time.time()-t0:.2f}s")
        
        t0 = time.time()
        logger.info("Starting OCR detection for space names...")
        space_names = detect_space_names(numpy.array(original_image))
        
        for space in space_names:
            # Process OCR Coordinates correctly for PaddleOCR format
            ix, iy = space['insertion_point']
            space['center_mm'] = {
                'x': float(pixels_to_mm(ix, scale_factor_mm_per_pixel)),
                'y': float(pixels_to_mm(iy, scale_factor_mm_per_pixel))
            }
            
            # PaddleOCR returns 4 corners: [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            pts = numpy.array(space['bbox'])
            x_coords = pts[:, 0]
            y_coords = pts[:, 1]
            x1, y1, x2, y2 = min(x_coords), min(y_coords), max(x_coords), max(y_coords)

            space['bbox_mm'] = {
                'x1': float(pixels_to_mm(x1, scale_factor_mm_per_pixel)),
                'y1': float(pixels_to_mm(y1, scale_factor_mm_per_pixel)),
                'x2': float(pixels_to_mm(x2, scale_factor_mm_per_pixel)),
                'y2': float(pixels_to_mm(y2, scale_factor_mm_per_pixel))
            }
        
        logger.info(f"OCR detected {len(space_names)} space names in {time.time()-t0:.2f}s")

        # ── Room polygon extraction ───────────────────────────────────────────
        # Runs on the same combined_wall_mask used for wall analysis.
        # space_names are passed so OCR labels get bound to the correct room polygon.
        t0 = time.time()
        room_polygons = extract_room_polygons(
            combined_wall_mask,
            scale_factor_mm_per_pixel,
            space_names=space_names
        )
        logger.info(f"Room analysis: {len(room_polygons)} rooms extracted in {time.time()-t0:.2f}s")

        # ── Host wall assignment for doors and windows ────────────────────────
        # Dynamo needs to know which wall hosts each door/window to place the
        # family instance correctly. Find the nearest wall centerline for each.
        for door in detailed_doors:
            ip = [door["location"]["center"]["x"], door["location"]["center"]["y"]]
            door["host_wall_id"] = find_host_wall_id(ip, wall_parameters)

        for win in detailed_windows:
            ip = [win["location"]["center"]["x"], win["location"]["center"]["y"]]
            win["host_wall_id"] = find_host_wall_id(ip, wall_parameters)
        
        t0 = time.time()
        vis_image = create_wall_visualization(original_image, r, wall_parameters, junction_analysis, w, h, scale_factor_mm_per_pixel, exterior_walls, space_names)
        logger.debug(f"Time - visualization drawing: {time.time()-t0:.2f}s")
        logger.info("Visualization image drawn; saving files …")
        
        test_num = getNextTestNumber()
        
        wall_vis_filename = f"vis{test_num}.png"
        wall_vis_filepath = os.path.join(IMAGES_OUTPUT_DIR, wall_vis_filename)
        vis_image.save(wall_vis_filepath)

        # Extract stair and slab geometries from model output
        bim_stairs = []
        bim_slabs = []
        for idx, cid in enumerate(r['class_ids']):
            if cid == 4 and 'masks' in r:  # Stairs
                mask = r['masks'][:, :, idx]
                stair_data = extract_stair_footprint(mask)
                if stair_data:
                    stair_data["id"] = f"Stair_{len(bim_stairs)+1}"
                    stair_data["base_level"] = 0.0
                    stair_data["top_level"] = 2800.0
                    bim_stairs.append(stair_data)
            elif cid in [5, 6, 7] and 'masks' in r:  # Parking, Balcony, Terrace
                mask = r['masks'][:, :, idx]
                polygon = extract_slab_polygon(mask)
                if len(polygon) >= 3:
                    name_map = {5: "Parking", 6: "Balcony", 7: "Terrace"}
                    bim_slabs.append({
                        "id": f"Slab_{len(bim_slabs)+1}",
                        "type": name_map[cid],
                        "thickness": 150.0,
                        "elevation": 0.0,
                        "polygon": polygon
                    })
        
        # Build unified JSON combining BIM data and OCR
        wall_analysis = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "image_dimensions": {"width": w, "height": h},
                "scale_factor_mm_per_pixel": scale_factor_mm_per_pixel,
                "analysis_type": "comprehensive_floor_plan_analysis",
                "units": "millimeters"
            },
            "bim_data": {
                "description": "Geometric vector data ready for Revit/BIM modeling via Dynamo",
                "coordinate_system": {
                    "origin": [0.0, 0.0, 0.0],
                    "units": "millimeters",
                    "level_elevation": 0.0,
                    "note": "All coordinates are relative to image top-left corner"
                },
                "walls": [
                    {
                        "id": wall["wall_id"],
                        "start_point":  [wall["centerline"][0][0],  wall["centerline"][0][1],  0.0],
                        "end_point":    [wall["centerline"][-1][0], wall["centerline"][-1][1], 0.0],
                        "thickness":    wall["thickness"]["average"],
                        "height":       2800.0,
                        "type":         f"Basic Wall - {int(wall['thickness']['average'])}mm",
                        "is_exterior":  wall["wall_id"] in [ew.get("wall_id") for ew in exterior_walls]
                    } for wall in wall_parameters if len(wall["centerline"]) >= 2
                ],
                "doors": [
                    {
                        "id":              f"Door_{d['door_id']}",
                        "host_wall_id":    d.get("host_wall_id"),
                        "insertion_point": [d["location"]["center"]["x"],
                                            d["location"]["center"]["y"], 0.0],
                        "width":           d["dimensions"]["width"],
                        "height":          2100.0,
                        "swing_angle":     d.get("swing_angle", 0.0),
                        "hinge_side":      d["orientation"].get("hinge_side", "unknown"),
                        "type":            "Single-Flush"
                    } for d in detailed_doors
                ],
                "windows": [
                    {
                        "id":              f"Window_{win['window_id']}",
                        "host_wall_id":    win.get("host_wall_id"),
                        "insertion_point": [win["location"]["center"]["x"],
                                            win["location"]["center"]["y"], 0.0],
                        "width":           win["dimensions"]["width"],
                        "height":          1200.0,
                        "sill_height":     900.0,
                        "type":            win["window_type"].capitalize() + " Window"
                    } for win in detailed_windows
                ],
                "rooms": room_polygons,
                "stairs": bim_stairs,
                "slabs":  bim_slabs
            },
            "summary": {
                "walls": {
                    "total_walls": len(wall_parameters),
                    "total_junctions": len(junction_analysis),
                    "total_length_mm": sum(w["length"] for w in wall_parameters),
                    "average_thickness_mm": sum(w["thickness"]["average"] for w in wall_parameters) / len(wall_parameters) if wall_parameters else 0
                },
                "doors": {
                    "total_doors": len(detailed_doors),
                    "average_confidence": float(numpy.mean([d["confidence"] for d in detailed_doors])) if detailed_doors else 0,
                    "door_orientations": {
                        "horizontal": sum(1 for d in detailed_doors if d["orientation"]["door_type"] == "horizontal"),
                        "vertical": sum(1 for d in detailed_doors if d["orientation"]["door_type"] == "vertical")
                    },
                    "swing_directions": {}
                },
                "windows": {
                    "total_windows": len(detailed_windows),
                    "average_confidence": float(numpy.mean([d["confidence"] for d in detailed_windows])) if detailed_windows else 0,
                    "window_types": {
                        "horizontal": sum(1 for d in detailed_windows if d["window_type"] == "horizontal"),
                        "vertical": sum(1 for d in detailed_windows if d["window_type"] == "vertical")
                    },
                    "glazing_types": {}
                },
                "space_names": {
                    "total_spaces_detected": len(space_names),
                    "average_confidence": float(numpy.mean([s["confidence"] for s in space_names])) if space_names else 0
                },
                "rooms": {
                    "total_rooms": len(room_polygons),
                    "total_area_m2": round(sum(r["area_m2"] for r in room_polygons), 2)
                }
            },
            "walls": {
                "individual_walls": wall_parameters,
                "junctions": junction_analysis
            },
            "doors": {
                "detailed_doors": detailed_doors
            },
            "windows": {
                "detailed_windows": detailed_windows
            },
            "space_names": {
                "total_spaces_detected": len(space_names),
                "spaces": space_names,
                "detection_summary": {
                    "average_confidence": float(numpy.mean([s["confidence"] for s in space_names])) if space_names else 0,
                    "confidence_range": {
                        "min": float(min([s["confidence"] for s in space_names])) if space_names else 0,
                        "max": float(max([s["confidence"] for s in space_names])) if space_names else 0
                    },
                    "detection_methods": ["PaddleOCR"],
                    "centerpoints_mm": [s["center_mm"] for s in space_names],
                    "centerpoints_px": [s["insertion_point"] for s in space_names]
                }
            }
        }
        
        for door in detailed_doors:
            swing = door["orientation"]["estimated_swing"]
            wall_analysis["summary"]["doors"]["swing_directions"][swing] = wall_analysis["summary"]["doors"]["swing_directions"].get(swing, 0) + 1
        
        for window in detailed_windows:
            glazing = window["architectural_analysis"]["glazing_type"]
            wall_analysis["summary"]["windows"]["glazing_types"][glazing] = wall_analysis["summary"]["windows"]["glazing_types"].get(glazing, 0) + 1
        
        wall_json_filename = f"final{test_num}.json"
        save_wall_analysis(wall_analysis, wall_json_filename)
        
        memory_after = check_memory_usage()
        logger.debug(f"Memory after processing: {memory_after:.1f}MB")
        
        return jsonify({
            "message": "Comprehensive floor plan analysis completed successfully",
            "visualization_file": wall_vis_filename,
            "analysis_file": wall_json_filename,
            "image_processing": {
                "original_size": resize_info["original_size"],
                "processed_size": resize_info.get("new_size", resize_info["original_size"]),
                "resized": resize_info["resized"],
                "resize_factor": resize_info["resize_factor"],
                "resize_reason": resize_info["reason"],
                "scale_factor_adjusted": resize_info["resized"],
                "original_scale_factor": scale_factor_mm_per_pixel / resize_info["resize_factor"] if resize_info["resized"] else scale_factor_mm_per_pixel,
                "final_scale_factor": scale_factor_mm_per_pixel
            },
            "memory_usage": {
                "before_processing_mb": memory_before,
                "after_processing_mb": memory_after,
                "memory_increase_mb": memory_after - memory_before
            },
            "total_walls": len(wall_parameters),
            "total_doors": len(detailed_doors),
            "total_windows": len(detailed_windows),
            "total_rooms": len(room_polygons),
            "total_junctions": len(junction_analysis),
            "total_space_names": len(space_names),
            "comprehensive_summary": {
                "wall_count": len(wall_parameters),
                "door_count": len(detailed_doors),
                "window_count": len(detailed_windows),
                "room_count": len(room_polygons),
                "junction_count": len(junction_analysis),
                "space_name_count": len(space_names),
                "total_wall_length_mm": sum(w["length"] for w in wall_parameters),
                "total_wall_thickness_mm": sum(w["thickness"]["average"] for w in wall_parameters),
                "perimeter_length_mm": perimeter_dimensions["total_perimeter_length"],
                "perimeter_area_mm2": perimeter_dimensions["perimeter_area"],
                "total_floor_area_m2": round(sum(r["area_m2"] for r in room_polygons), 2)
            }
        })
        
    except Exception as e:
        logger.error("Error in wall visualization: %s", e, exc_info=True)
        raise