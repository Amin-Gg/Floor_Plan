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

from models.mask_rcnn_model import get_model, is_model_initialized
from services.image_validation import validate_and_resize_image, check_memory_usage
from image_processing.image_loader import myImageLoader

from utils.error_handlers import ModelNotReadyError, ImageValidationError
from utils.validators import require_image_upload, validate_scale_factor
from utils.inference_executor import get_executor
from utils.geometry import safe_logical_or, safe_logical_and
from utils.conversions import (
    pixels_to_mm, pixels_sq_to_mm_sq, convert_junction_position_to_mm, save_wall_analysis)
from utils.polygon_geometry import polygon_area_m2, polygon_perimeter_m

from analysis.room_analysis import extract_room_polygons, find_host_wall_id
from utils.file_utils import getNextTestNumber
from schemas import AnalyzeFormRequest
from services.analysis_report import AnalysisReport
from services.bim_builder    import BimDataBuilder

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

    # Tracker for the analysis_report block returned in the response.
    # Recording into this object never affects route behavior — it's pure
    # additive metadata. If anything inside the tracker fails it swallows
    # its own errors (see services/analysis_report.py).
    report = AnalysisReport()
    report.set_model_mode("fine_tuned" if os.getenv("FLOORPLAN_MODEL_PATH") else "coco_fallback")

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
        report.set_stage("building_params", "ok")
    except (ValueError, TypeError) as _bp_err:
        building_params = {}
        report.set_stage("building_params", "degraded",
                         f"invalid JSON, using defaults ({_bp_err})")
        report.add_warning("building_params was not valid JSON — defaults applied")
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
            # When an image is scaled DOWN by resize_factor (e.g. 0.5),
            # each surviving pixel represents MORE real-world distance.
            # Correct formula: mm_per_px = original_mm_per_px / resize_factor
            # (NOT *=, which would make mm/px smaller — the wrong direction)
            scale_factor_mm_per_pixel = original_scale / resize_info["resize_factor"]
            logger.info(
                "Scale factor adjusted for resize: %.4f → %.4f "
                "(resize_factor=%.3f)",
                original_scale, scale_factor_mm_per_pixel, resize_info["resize_factor"]
            )
        
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
            report.add_warning(
                f"Input image was downsampled from "
                f"{resize_info['original_size']} to {resize_info['new_size']} "
                f"(reason: {resize_info.get('reason', 'size_limit')})"
            )
        
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
        # OCR is wrapped in try/except so PaddleOCR failures (missing model
        # files, GPU OOM, library install issues) degrade gracefully to an
        # empty list instead of failing the whole /analyze request. Room
        # extraction and host-wall resolution already handle empty space_names.
        try:
            space_names = detect_space_names(numpy.array(original_image))
            report.set_ocr_used(True)
            report.set_stage("ocr", "ok")
        except Exception as _ocr_err:
            logger.warning("OCR failed (continuing without space names): %s", _ocr_err, exc_info=True)
            space_names = []
            report.set_ocr_used(False)
            report.set_stage("ocr", "failed", str(_ocr_err))
            report.add_warning("OCR engine failed — room names will not be available")

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
        report.set_stage("rooms", "ok")

        # ── Host wall assignment for doors and windows ────────────────────────
        # Dynamo needs to know which wall hosts each door/window to place the
        # family instance correctly. Find the nearest wall centerline for each.
        # If a door/window has no host wall, it's recorded in the report as
        # skipped from BIM hosting — the element still appears in the response
        # but won't place correctly in Revit/Dynamo without a host.
        doors_without_host = 0
        for i, door in enumerate(detailed_doors):
            ip = [door["location"]["center"]["x"], door["location"]["center"]["y"]]
            host_id = find_host_wall_id(ip, wall_parameters)
            door["host_wall_id"] = host_id
            if host_id is None:
                doors_without_host += 1
                report.add_skipped("door", "no host wall could be assigned",
                                   element_id=f"Door_{i+1}")

        windows_without_host = 0
        for i, win in enumerate(detailed_windows):
            ip = [win["location"]["center"]["x"], win["location"]["center"]["y"]]
            host_id = find_host_wall_id(ip, wall_parameters)
            win["host_wall_id"] = host_id
            if host_id is None:
                windows_without_host += 1
                report.add_skipped("window", "no host wall could be assigned",
                                   element_id=f"Window_{i+1}")

        if doors_without_host or windows_without_host:
            report.set_stage(
                "host_walls", "degraded",
                f"{doors_without_host} door(s) and {windows_without_host} window(s) "
                f"have no host wall assignment"
            )
        else:
            report.set_stage("host_walls", "ok")
        
        t0 = time.time()
        vis_image = create_wall_visualization(original_image, r, wall_parameters, junction_analysis, w, h, scale_factor_mm_per_pixel, exterior_walls, space_names)
        logger.debug(f"Time - visualization drawing: {time.time()-t0:.2f}s")
        logger.info("Visualization image drawn; saving files …")
        
        test_num = getNextTestNumber()
        
        wall_vis_filename = f"vis{test_num}.png"
        wall_vis_filepath = os.path.join(IMAGES_OUTPUT_DIR, wall_vis_filename)
        vis_image.save(wall_vis_filepath)

        # Single source of truth for building height parameters.
        # The builder validates and falls back to industry-standard defaults
        # if any field is missing or malformed; see services/bim_builder.py.
        bim_builder = BimDataBuilder(building_params)
        WALL_H     = bim_builder.wall_height
        DOOR_H     = bim_builder.door_height
        WIN_SILL   = bim_builder.window_sill
        WIN_H      = bim_builder.window_height
        SLAB_THICK = bim_builder.floor_thickness

        # Extract stair and slab geometries — convert pixel coords to mm.
        # Detected masks that don't produce valid geometry (e.g. fragmented
        # masks, polygons with < 3 points) are recorded in the report as
        # skipped rather than silently dropped.
        bim_stairs = []
        bim_slabs  = []
        # New element buckets for classes 8-15 (room types, railing, closet).
        # These are CAPTURED here so they reach the API response, but the
        # detailed BIM modeling (IfcSpace type derivation, railing extrusion
        # direction, closet host-room assignment) is intentionally deferred
        # until the model is trained on these classes and we can design the
        # downstream BIM logic against real predictions.
        # Until training completes for classes 8-15, these lists will be empty.
        bim_ml_rooms      = []   # room_* classes (project IDs 8-13)
        bim_railings      = []   # railing class (project ID 14)
        bim_closets       = []   # closet class (project ID 15)

        stairs_detected_in_masks = 0
        slabs_detected_in_masks  = 0
        ml_rooms_detected        = 0
        railings_detected        = 0
        closets_detected         = 0

        # Map project IDs 8-13 → human-readable room type names (used in BIM output)
        _ROOM_TYPE_MAP = {
            8:  "Bedroom",
            9:  "LivingRoom",
            10: "Kitchen",
            11: "Bathroom",
            12: "Entry",
            13: "Storage",
        }
        for idx, cid in enumerate(r['class_ids']):
            if cid == 4 and 'masks' in r:   # Stairs
                stairs_detected_in_masks += 1
                mask = r['masks'][:, :, idx]
                stair_data = extract_stair_footprint(mask)
                if stair_data:
                    corners_mm = [
                        [pt[0] * scale_factor_mm_per_pixel,
                         pt[1] * scale_factor_mm_per_pixel]
                        for pt in stair_data["corners"]
                    ]
                    corners_mm.append(corners_mm[0])   # close polygon
                    bim_stairs.append({
                        "id":               f"Stair_{len(bim_stairs)+1}",
                        "footprint_polygon": corners_mm,
                        "center_mm": [
                            stair_data["center"][0] * scale_factor_mm_per_pixel,
                            stair_data["center"][1] * scale_factor_mm_per_pixel,
                        ],
                        "width_mm":        stair_data["dimensions"]["width"]  * scale_factor_mm_per_pixel,
                        "length_mm":       stair_data["dimensions"]["length"] * scale_factor_mm_per_pixel,
                        "rotation_angle":  stair_data["rotation_angle"],
                        "base_level":      0.0,
                        "top_level":       WALL_H,
                        # area_m2 / perimeter_m are auto-computed for validation
                        # and for the client to display without parsing geometry.
                        "area_m2":         round(polygon_area_m2(corners_mm), 2),
                        "perimeter_m":     round(polygon_perimeter_m(corners_mm), 2),
                    })
                else:
                    report.add_skipped("stair",
                                       "extract_stair_footprint returned no valid geometry",
                                       element_id=f"detection_{idx}")
            elif cid in [5, 6, 7] and 'masks' in r:   # Parking, Balcony, Terrace
                slabs_detected_in_masks += 1
                mask = r['masks'][:, :, idx]
                polygon_px = extract_slab_polygon(mask)
                if len(polygon_px) >= 3:
                    polygon_mm = [
                        [pt[0] * scale_factor_mm_per_pixel,
                         pt[1] * scale_factor_mm_per_pixel]
                        for pt in polygon_px
                    ]
                    # Ensure polygon is explicitly closed (first == last point)
                    if polygon_mm[0] != polygon_mm[-1]:
                        polygon_mm.append(polygon_mm[0])
                    name_map = {5: "Parking", 6: "Balcony", 7: "Terrace"}
                    bim_slabs.append({
                        "id":        f"Slab_{len(bim_slabs)+1}",
                        "type":      name_map[cid],
                        "polygon":   polygon_mm,
                        "thickness": SLAB_THICK,
                        "elevation": 0.0,
                        # area_m2 / perimeter_m are auto-computed for validation
                        # and for the client to display without parsing geometry.
                        "area_m2":     round(polygon_area_m2(polygon_mm), 2),
                        "perimeter_m": round(polygon_perimeter_m(polygon_mm), 2),
                    })
                else:
                    slab_type = {5: "parking", 6: "balcony", 7: "terrace"}.get(cid, "slab")
                    report.add_skipped(slab_type,
                                       f"polygon had fewer than 3 points ({len(polygon_px)})",
                                       element_id=f"detection_{idx}")

            # ── New classes (8-15) — placeholder capture ─────────────────────
            # Until the model is trained for these classes, this branch is
            # cold code: no predictions arrive with cid >= 8. After training,
            # this captures them into the appropriate bucket so the response
            # is complete; the downstream BIM/IFC modeling for these will be
            # designed in a follow-up iteration.
            elif cid in (8, 9, 10, 11, 12, 13) and 'masks' in r:   # Room types
                ml_rooms_detected += 1
                mask = r['masks'][:, :, idx]
                polygon_px = extract_slab_polygon(mask)   # same polygon extractor
                if len(polygon_px) >= 3:
                    polygon_mm = [
                        [pt[0] * scale_factor_mm_per_pixel,
                         pt[1] * scale_factor_mm_per_pixel]
                        for pt in polygon_px
                    ]
                    if polygon_mm[0] != polygon_mm[-1]:
                        polygon_mm.append(polygon_mm[0])
                    bim_ml_rooms.append({
                        "id":          f"MLRoom_{len(bim_ml_rooms)+1}",
                        "room_type":   _ROOM_TYPE_MAP.get(cid, "Unknown"),
                        "polygon":     polygon_mm,
                        "area_m2":     round(polygon_area_m2(polygon_mm), 2),
                        "perimeter_m": round(polygon_perimeter_m(polygon_mm), 2),
                        # source field distinguishes ML detections from the
                        # flood-fill rooms in bim_data["rooms"]. The client
                        # (or a future merge step) can decide which to trust.
                        "source":      "ml_detection",
                    })
                else:
                    room_type = _ROOM_TYPE_MAP.get(cid, "room").lower()
                    report.add_skipped(room_type,
                                       f"polygon had fewer than 3 points ({len(polygon_px)})",
                                       element_id=f"detection_{idx}")
            elif cid == 14 and 'masks' in r:   # Railing
                railings_detected += 1
                mask = r['masks'][:, :, idx]
                polygon_px = extract_slab_polygon(mask)
                if len(polygon_px) >= 2:   # railings can be just a line
                    polygon_mm = [
                        [pt[0] * scale_factor_mm_per_pixel,
                         pt[1] * scale_factor_mm_per_pixel]
                        for pt in polygon_px
                    ]
                    bim_railings.append({
                        "id":          f"Railing_{len(bim_railings)+1}",
                        "polygon":     polygon_mm,
                        "perimeter_m": round(polygon_perimeter_m(polygon_mm), 2),
                        # Detailed properties (height, host element, top_level)
                        # are deferred — Iranian building code typically requires
                        # 1100mm railing height for balconies above 1m elevation.
                    })
                else:
                    report.add_skipped("railing",
                                       f"polygon too small ({len(polygon_px)} points)",
                                       element_id=f"detection_{idx}")
            elif cid == 15 and 'masks' in r:   # Closet
                closets_detected += 1
                mask = r['masks'][:, :, idx]
                polygon_px = extract_slab_polygon(mask)
                if len(polygon_px) >= 3:
                    polygon_mm = [
                        [pt[0] * scale_factor_mm_per_pixel,
                         pt[1] * scale_factor_mm_per_pixel]
                        for pt in polygon_px
                    ]
                    if polygon_mm[0] != polygon_mm[-1]:
                        polygon_mm.append(polygon_mm[0])
                    bim_closets.append({
                        "id":          f"Closet_{len(bim_closets)+1}",
                        "polygon":     polygon_mm,
                        "area_m2":     round(polygon_area_m2(polygon_mm), 2),
                        "perimeter_m": round(polygon_perimeter_m(polygon_mm), 2),
                        # host_room_id assignment is deferred — needs a point-in-
                        # polygon test against the room polygons, designed once
                        # real predictions are available.
                    })
                else:
                    report.add_skipped("closet",
                                       f"polygon had fewer than 3 points ({len(polygon_px)})",
                                       element_id=f"detection_{idx}")

        # Stage status for stairs/slabs based on detect-vs-keep ratio
        if stairs_detected_in_masks > 0:
            if len(bim_stairs) == stairs_detected_in_masks:
                report.set_stage("stairs", "ok")
            elif len(bim_stairs) == 0:
                report.set_stage("stairs", "failed",
                                 f"all {stairs_detected_in_masks} stair masks failed geometry extraction")
            else:
                report.set_stage("stairs", "degraded",
                                 f"{stairs_detected_in_masks - len(bim_stairs)} of "
                                 f"{stairs_detected_in_masks} stair masks failed geometry extraction")
        else:
            report.set_stage("stairs", "skipped", "no stair detections in model output")

        if slabs_detected_in_masks > 0:
            if len(bim_slabs) == slabs_detected_in_masks:
                report.set_stage("slabs", "ok")
            elif len(bim_slabs) == 0:
                report.set_stage("slabs", "failed",
                                 f"all {slabs_detected_in_masks} slab masks failed geometry extraction")
            else:
                report.set_stage("slabs", "degraded",
                                 f"{slabs_detected_in_masks - len(bim_slabs)} of "
                                 f"{slabs_detected_in_masks} slab masks failed geometry extraction")
        else:
            report.set_stage("slabs", "skipped", "no slab detections in model output")

        # Stage status for the new classes (8-15). Until the model is trained
        # for these classes, all three will be "skipped" — that's expected
        # and intentional, not a problem.
        if ml_rooms_detected > 0:
            if len(bim_ml_rooms) == ml_rooms_detected:
                report.set_stage("ml_rooms", "ok")
            elif len(bim_ml_rooms) == 0:
                report.set_stage("ml_rooms", "failed",
                                 f"all {ml_rooms_detected} room masks failed geometry extraction")
            else:
                report.set_stage("ml_rooms", "degraded",
                                 f"{ml_rooms_detected - len(bim_ml_rooms)} of "
                                 f"{ml_rooms_detected} room masks failed geometry extraction")
        else:
            report.set_stage("ml_rooms", "skipped",
                             "no room detections in model output "
                             "(model not yet trained on room classes)")

        if railings_detected > 0:
            if len(bim_railings) == railings_detected:
                report.set_stage("railings", "ok")
            else:
                report.set_stage("railings", "degraded",
                                 f"{railings_detected - len(bim_railings)} of "
                                 f"{railings_detected} railing masks failed geometry extraction")
        else:
            report.set_stage("railings", "skipped",
                             "no railing detections in model output "
                             "(model not yet trained on railing class)")

        if closets_detected > 0:
            if len(bim_closets) == closets_detected:
                report.set_stage("closets", "ok")
            else:
                report.set_stage("closets", "degraded",
                                 f"{closets_detected - len(bim_closets)} of "
                                 f"{closets_detected} closet masks failed geometry extraction")
        else:
            report.set_stage("closets", "skipped",
                             "no closet detections in model output "
                             "(model not yet trained on closet class)")

        # Build unified JSON combining BIM data and OCR
        wall_analysis = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "image_dimensions": {"width": w, "height": h},
                "scale_factor_mm_per_pixel": scale_factor_mm_per_pixel,
                "analysis_type": "comprehensive_floor_plan_analysis",
                "units": "millimeters"
            },
            "bim_data": bim_builder.build(
                wall_parameters  = wall_parameters,
                detailed_doors   = detailed_doors,
                detailed_windows = detailed_windows,
                room_polygons    = room_polygons,
                bim_stairs       = bim_stairs,
                bim_slabs        = bim_slabs,
                exterior_walls   = exterior_walls,
            ),
            # New ML-detected element buckets — separate from bim_data so the
            # existing client integration doesn't break. Empty lists are normal
            # until the model is trained on the new classes.
            "ml_detections": {
                "rooms":    bim_ml_rooms,
                "railings": bim_railings,
                "closets":  bim_closets,
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
        
        # Finalize the analysis_report with final element counts.
        # Wrapped in try/except so report-building errors NEVER fail the request.
        try:
            report.set_elements({
                "walls":       len(wall_parameters),
                "doors":       len(detailed_doors),
                "windows":     len(detailed_windows),
                "rooms":       len(room_polygons),
                "junctions":   len(junction_analysis),
                "stairs":      len(bim_stairs),
                "slabs":       len(bim_slabs),
                "space_names": len(space_names),
                # New element counts (classes 8-15). Always 0 until the model
                # is trained on these classes — that's expected and intentional.
                "ml_rooms":    len(bim_ml_rooms),
                "railings":    len(bim_railings),
                "closets":     len(bim_closets),
            })
            analysis_report_block = report.to_dict()
        except Exception as _rep_err:
            logger.warning("Failed to finalize analysis_report (%s)", _rep_err, exc_info=True)
            analysis_report_block = {
                "model_mode": "unknown",
                "elements":   {},
                "stages":     {},
                "skipped":    [],
                "warnings":   ["analysis_report finalization failed"],
            }

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
            },
            "analysis_report": analysis_report_block,
        })
        
    except Exception as e:
        logger.error("Error in wall visualization: %s", e, exc_info=True)
        raise