"""
Visualization routes for wall analysis
"""

import logging
import os
import time
from datetime import datetime

import cv2
import numpy
from flask import g, jsonify, request
from flask_openapi3 import APIBlueprint, Tag

from analysis.door_analysis import (
    analyzeDoorOrientation,
    assess_door_accessibility,
    categorize_door_size,
    generateArchitecturalNotes,
)
from analysis.junction_analysis import find_junctions_from_bboxes
from analysis.room_analysis import assess_host_wall, extract_room_polygons
from analysis.wall_analysis import (
    analyze_junction_types,
    calculate_perimeter_dimensions,
    extract_wall_parameters,
    find_wall_connections,
    identify_exterior_walls,
)
from analysis.window_analysis import (
    analyze_window_geometry,
    assess_window_glazing,
    categorize_window_size,
    generate_window_notes,
)
from config.constants import IMAGES_OUTPUT_DIR
from image_processing.image_loader import myImageLoader
from image_processing.mask_processing import extract_wall_masks, segment_individual_walls
from services.model_runtime import is_runtime_ready
from ocr_detector import detect_space_names
from services.analysis_report import AnalysisReport
from services.bim_builder import BimDataBuilder
from services.detection_pipeline import run_detectors
from services.image_validation import check_memory_usage, validate_and_resize_image
from services.preprocessing import decide_office_enhancement
from services.yolo_elements import build_yolo_elements
from schemas import AnalyzeFormRequest, AnalyzeResponse, ErrorResponse
from utils.conversions import convert_junction_position_to_mm, pixels_to_mm, save_wall_analysis
from utils.error_handlers import ImageValidationError, ModelNotReadyError, ValidationError
from utils.file_utils import getNextTestNumber
from utils.geometry import safe_logical_and, safe_logical_or
from utils.validators import require_image_upload, validate_scale_factor
from visualization.wall_visualization import create_wall_visualization

logger = logging.getLogger(__name__)

bp = APIBlueprint('visualization', __name__)
TAG = Tag(name='Core', description='Floor-plan image analysis and BIM generation')


def _orientation_to_angle(orientation: dict):
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
    return swing_map.get(swing)

@bp.post(
    '/analyze',
    tags=[TAG],
    summary='Analyze a floor-plan image',
    responses={200: AnalyzeResponse, 400: ErrorResponse, 422: ErrorResponse, 503: ErrorResponse},
)
def analyze_floor_plan(form: AnalyzeFormRequest):
    """Create enhanced visualization showing wall centerlines, junctions, and wall parameters"""

    if not is_runtime_ready():
        raise ModelNotReadyError()

    # Tracker for the analysis_report block returned in the response.
    # Recording into this object never affects route behavior — it's pure
    # additive metadata. If anything inside the tracker fails it swallows
    # its own errors (see services/analysis_report.py).
    report = AnalysisReport()
    # The final mode is recorded after detector orchestration so the response
    # states whether the optional YOLO extension actually ran for this request.
    report.set_model_mode("mask_rcnn_4class")

    imagefile = require_image_upload("image")

    # Legacy public-boundary compatibility: flat building_params and free-form scale_source
    # are removed because they bypass the shared versioned contracts. A scalar
    # scale factor without evidence is accepted only as unverified legacy input.
    if request.form.get("building_params") not in (None, "", "{}"):
        raise ValidationError(
            "building_params was removed; use manual_inputs schema_version 1.0",
            details={"contract": "contracts/manual_inputs_v1.json"},
        )
    if request.form.get("scale_source") not in (None, ""):
        raise ValidationError(
            "scale_source was removed; send scale_evidence schema_version 1.0",
            details={"contract": "contracts/scale_evidence_v1.json"},
        )

    from stage1_contracts import (
        ManualInputsError,
        ScaleEvidenceError,
        assess_scale_evidence,
        parse_manual_inputs,
    )
    try:
        manual_inputs = parse_manual_inputs(request.form.get("manual_inputs", "{}"))
    except ManualInputsError as exc:
        raise ValidationError(str(exc), details={"field": "manual_inputs"}) from exc

    scale_evidence_raw = request.form.get("scale_evidence")
    if scale_evidence_raw in (None, ""):
        legacy_mmpp = validate_scale_factor(
            request.form.get("scale_factor_mm_per_pixel", 1.0)
        )
        scale_evidence_raw = {
            "schema_version": "1.0",
            "mm_per_pixel": legacy_mmpp,
            "source": "default_unverified",
            "evidence": [],
        }
    try:
        scale_block = assess_scale_evidence(scale_evidence_raw)
    except ScaleEvidenceError as exc:
        raise ValidationError(str(exc), details={"field": "scale_evidence"}) from exc
    scale_factor_mm_per_pixel = scale_block["mm_per_pixel"]

    defaults = manual_inputs.get("defaults", {})
    project_inputs = manual_inputs.get("project", {})
    building_params = {
        "wall_height": defaults.get(
            "wall_height_mm",
            defaults.get("ceiling_height_mm", project_inputs.get("default_storey_height_mm", 2800.0)),
        ),
        "door_height": defaults.get("door_height_mm", 2100.0),
        "window_height": defaults.get("window_height_mm", 1200.0),
        "window_sill_height": defaults.get("window_sill_height_mm", 900.0),
        "floor_thickness": defaults.get(
            "floor_thickness_mm", project_inputs.get("floor_thickness_mm", 200.0)
        ),
    }
    report.set_stage("manual_inputs", "ok")
    report.set_stage("scale_evidence", "degraded" if scale_block["needs_review"] else "ok")

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

        preprocess_decision = decide_office_enhancement(
            original_image,
            exif_transposed=bool(resize_info.get("exif_transposed", False)),
        )
        image, w, h = myImageLoader(
            imagefile,
            enhance_for_office=preprocess_decision.office_enhancement_applied,
        )
        report.set_stage(
            "preprocessing",
            "degraded" if preprocess_decision.office_enhancement_applied else "ok",
            preprocess_decision.reason,
        )
        logger.info(
            "Creating wall analysis visualization for image: %dx%d "
            "(office_enhancement=%s, edge_density=%.6f)",
            h, w, preprocess_decision.office_enhancement_applied,
            preprocess_decision.edge_density,
        )

        if resize_info["resized"]:
            logger.info(f"Image was resized: {resize_info['original_size']} -> {resize_info['new_size']}")
            report.add_warning(
                f"Input image was downsampled from "
                f"{resize_info['original_size']} to {resize_info['new_size']} "
                f"(reason: {resize_info.get('reason', 'size_limit')})"
            )

        t0 = time.time()
        detector_output = run_detectors(image)
        r = detector_output["primary"]
        yolo_detections = detector_output["supplementary"]
        detector_status = detector_output["detector_status"]
        report.set_model_mode(
            "mask_rcnn_4class+yolo_supplementary"
            if detector_status["supplementary"] == "yolo_v8"
            else "mask_rcnn_4class"
        )
        report.set_stage(
            "supplementary_detector",
            "ok" if detector_status["supplementary"] == "yolo_v8" else "skipped",
            detector_status["supplementary_status"],
        )
        logger.debug("Time - detector pipeline: %.2fs", time.time() - t0)

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

                # The opening width follows the long axis of the plan symbol.
                # Vertical dimensions are not observable in plan view and come
                # from the resolved Manual Inputs contract instead.
                if orientation.get("door_type") == "horizontal":
                    width_px = abs(x2 - x1)
                    symbol_minor_px = abs(y2 - y1)
                else:
                    width_px = abs(y2 - y1)
                    symbol_minor_px = abs(x2 - x1)
                width_mm = float(pixels_to_mm(width_px, scale_factor_mm_per_pixel))
                symbol_minor_mm = float(
                    pixels_to_mm(symbol_minor_px, scale_factor_mm_per_pixel)
                )
                resolved_door_height = float(building_params["door_height"])

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
                        "width_source": "mask_long_axis",
                        "height": resolved_door_height,
                        "height_source": "manual_inputs",
                        "plan_symbol_minor_axis_mm": symbol_minor_mm,
                    },
                    "orientation": orientation,
                    "swing_angle": _orientation_to_angle(orientation),
                    "architectural_analysis": {
                        "door_type": "not_observable_from_plan",
                        "size_category": categorize_door_size(
                            width_mm, resolved_door_height
                        ),
                        "accessibility": assess_door_accessibility(width_mm),
                        "notes": architectural_notes,
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
                window_geometry = analyze_window_geometry(window_mask, bbox)
                width_px = window_geometry["major_axis_px"]
                symbol_minor_px = window_geometry["minor_axis_px"]
                window_type = window_geometry["orientation"]
                width_mm = float(pixels_to_mm(width_px, scale_factor_mm_per_pixel))
                symbol_minor_mm = float(
                    pixels_to_mm(symbol_minor_px, scale_factor_mm_per_pixel)
                )
                resolved_window_height = float(building_params["window_height"])

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
                        "width_source": window_geometry["geometry_source"],
                        "height": resolved_window_height,
                        "height_source": "manual_inputs",
                        "plan_symbol_minor_axis_mm": symbol_minor_mm,
                        "rotation_angle_deg": window_geometry["rotation_angle_deg"],
                    },
                    "window_type": window_type,
                    "architectural_analysis": {
                        "size_category": categorize_window_size(
                            width_mm, resolved_window_height
                        ),
                        "glazing_type": assess_window_glazing(),
                        "notes": generate_window_notes(
                            width_mm, resolved_window_height, window_type
                        ),
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
        exterior_by_id = {
            wall.get("wall_id"): wall
            for wall in exterior_walls
            if isinstance(wall, dict) and wall.get("wall_id") is not None
        }

        doors_without_host = 0
        for i, door in enumerate(detailed_doors):
            ip = [door["location"]["center"]["x"], door["location"]["center"]["y"]]
            hb = assess_host_wall(ip, wall_parameters)        # Issue 6
            door["host_wall_id"] = hb["host_wall_id"]
            door["host_wall_confidence"] = hb["host_wall_confidence"]
            door["host_wall_distance_mm"] = hb["host_wall_distance_mm"]
            door["candidate_host_walls"] = hb["candidate_host_walls"]
            ext = exterior_by_id.get(hb["host_wall_id"])
            door["is_exterior"] = ext is not None
            door["externality_source"] = "host_wall_classification"
            door["externality_confidence"] = (
                float(ext.get("exterior_confidence", 1.0)) if ext else 1.0
            )
            orientation_review = bool(door.get("orientation", {}).get("needs_review"))
            reasons = [
                x for x in (
                    hb["review_reason"],
                    door.get("orientation", {}).get("review_reason"),
                ) if x
            ]
            door["needs_review"] = bool(hb["needs_review"] or orientation_review)
            door["review_reason"] = "; ".join(reasons)
            if hb["host_wall_id"] is None:
                doors_without_host += 1
                report.add_skipped("door", "no host wall could be assigned",
                                   element_id=f"Door_{i+1}")

        windows_without_host = 0
        for i, win in enumerate(detailed_windows):
            ip = [win["location"]["center"]["x"], win["location"]["center"]["y"]]
            hb = assess_host_wall(ip, wall_parameters)        # Issue 6
            win["host_wall_id"] = hb["host_wall_id"]
            win["host_wall_confidence"] = hb["host_wall_confidence"]
            win["host_wall_distance_mm"] = hb["host_wall_distance_mm"]
            win["candidate_host_walls"] = hb["candidate_host_walls"]
            ext = exterior_by_id.get(hb["host_wall_id"])
            win["is_exterior"] = ext is not None
            win["externality_source"] = "host_wall_classification"
            win["externality_confidence"] = (
                float(ext.get("exterior_confidence", 1.0)) if ext else 1.0
            )
            win["needs_review"] = bool(
                hb["needs_review"] or (ext and ext.get("exterior_needs_review", False))
            )
            reasons = [hb["review_reason"]]
            if ext and ext.get("exterior_needs_review"):
                reasons.append("host wall exterior classification needs review")
            win["review_reason"] = "; ".join(x for x in reasons if x)
            if hb["host_wall_id"] is None:
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

        # Active detector contract:
        # - Mask R-CNN is authoritative for wall/window/door only.
        # - Optional YOLO may contribute approximate supplementary elements.
        #   Stair boxes are promoted to the canonical stairs bucket because the
        #   IFC exporter supports stairs. Columns/railings/curtain walls remain
        #   explicitly advisory until their IFC representations and geometry
        #   gates are implemented; they are never silently discarded.
        yolo_elements = build_yolo_elements(
            yolo_detections,
            scale_factor_mm_per_pixel,
            wall_height_mm=WALL_H,
            mask_covered_classes=frozenset(),
        )
        bim_stairs = list(yolo_elements["stairs"])
        bim_slabs = []
        advisory_elements = {
            "columns": yolo_elements["columns"],
            "railings": yolo_elements["railings"],
            "curtain_walls": yolo_elements["curtain_walls"],
            "contract_status": "advisory_not_exported",
            "reason": (
                "IFC export and independent geometry gates are not yet defined "
                "for these supplementary box-derived classes"
            ),
        }

        if detector_status["supplementary"] != "yolo_v8":
            report.set_stage(
                "stairs", "skipped",
                "supplementary YOLO detector is disabled or unavailable",
            )
        elif bim_stairs:
            report.set_stage("stairs", "degraded", "YOLO box-derived approximate geometry")
        else:
            report.set_stage("stairs", "skipped", "no mapped stair detections")
        report.set_stage("slabs", "skipped", "active detectors do not produce slab classes")

        # Build unified JSON combining BIM data and OCR
        wall_analysis = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "image_dimensions": {"width": w, "height": h},
                "scale_factor_mm_per_pixel": scale_factor_mm_per_pixel,
                "analysis_type": "comprehensive_floor_plan_analysis",
                "units": "millimeters",
                "preprocessing": preprocess_decision.to_dict(),
                "detectors": detector_status,
            },
            "bim_data": bim_builder.build(
                wall_parameters  = wall_parameters,
                detailed_doors   = detailed_doors,
                detailed_windows = detailed_windows,
                room_polygons    = room_polygons,
                bim_stairs       = bim_stairs,
                bim_slabs        = bim_slabs,
                exterior_walls   = exterior_walls,
                scale            = scale_block,
            ),
            "supplementary_detections": {
                "raw_mapped_detections": yolo_detections,
                "advisory_elements": advisory_elements,
                "promoted_to_canonical_stairs": len(bim_stairs),
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
                    "glazing_observability": "not_observable_from_plan"
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


        # Producer-side prevention layer: resolve manual values before IFC export and
        # stamp value-level provenance. Geometry Gate remains an independent
        # defensive layer for external, old, or tampered IFC files.
        from stage1_contracts import build_measurement_provenance, resolve_manual_inputs
        try:
            resolved_bim, manual_meta = resolve_manual_inputs(
                wall_analysis["bim_data"], manual_inputs
            )
        except ManualInputsError as exc:
            raise ValidationError(str(exc), details={"field": "manual_inputs"}) from exc
        wall_analysis["bim_data"] = build_measurement_provenance(
            resolved_bim,
            context={
                "request_id": getattr(g, "request_id", None),
                "model_version": "mask_rcnn_4class",
                "weight_version": os.path.basename(os.environ.get(
                    "MASK_RCNN_WEIGHTS", "maskrcnn_15_epochs.h5"
                )),
                "timestamp": wall_analysis["metadata"]["timestamp"],
            },
        )
        wall_analysis["metadata"]["manual_inputs_sha256"] = manual_meta["input_sha256"]
        wall_analysis["metadata"]["manual_inputs_resolved_sha256"] = manual_meta["resolved_sha256"]
        wall_analysis["metadata"]["scale_evidence_sha256"] = scale_block["evidence_sha256"]

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
                "yolo_columns_advisory": len(advisory_elements["columns"]),
                "yolo_railings_advisory": len(advisory_elements["railings"]),
                "yolo_curtain_walls_advisory": len(advisory_elements["curtain_walls"]),
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
            "success": True,
            "request_id": getattr(g, "request_id", "-"),
            "message": "Comprehensive floor plan analysis completed successfully",
            "visualization_file": wall_vis_filename,
            "analysis_file": wall_json_filename,
            "image_processing": {
                "original_size": resize_info["original_size"],
                "processed_size": resize_info.get("new_size", resize_info["original_size"]),
                "resized": resize_info["resized"],
                "resize_factor": resize_info["resize_factor"],
                "resize_reason": resize_info["reason"],
                "exif_transposed": resize_info.get("exif_transposed", False),
                "preprocessing": preprocess_decision.to_dict(),
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
            "bim_data": wall_analysis["bim_data"],
            "summary": wall_analysis["summary"],
        })

    except Exception as e:
        logger.error("Error in wall visualization: %s", e, exc_info=True)
        raise