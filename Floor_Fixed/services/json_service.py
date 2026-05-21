"""
JSON building and response formatting services
Enhanced with BIM/Revit compatible vector data export 
"""

import numpy
from datetime import datetime
from image_processing.image_loader import (
    calculateObjectArea, calculateObjectCenter, encodeMaskSummary, getClassName)
from analysis.door_analysis import enhancedDoorAnalysis
from analysis.wall_analysis import calculate_centered_straight_centerline, calculate_wall_thickness

# Import the new analysis modules
from analysis.stair_analysis import extract_stair_footprint
from analysis.slab_analysis import extract_slab_polygon

def get_extended_class_name(class_id):
    """Fallback mapping for new classes not yet in image_loader"""
    mapping = {4: "Stairs", 5: "Parking", 6: "Balcony", 7: "Terrace"}
    if class_id <= 3:
        return getClassName(class_id)
    return mapping.get(class_id, "Unknown")

def buildEnhancedJson(model_results, image_width, image_height, original_image,
                      building_params: dict = None):
    """
    Build the enriched JSON response from model detection results.

    Parameters
    ----------
    building_params : dict, optional
        Height/thickness overrides from the API request. Supported keys:
            wall_height        (mm) default 2800
            door_height        (mm) default 2100
            floor_thickness    (mm) default 200
            window_sill_height (mm) default 900
            window_height      (mm) default 1200
        If None or a key is absent, the default is used.
    """
    if building_params is None:
        building_params = {}

    # Resolve heights — API params override defaults
    WALL_HEIGHT     = float(building_params.get("wall_height",        2800.0))
    DOOR_HEIGHT     = float(building_params.get("door_height",        2100.0))
    FLOOR_THICKNESS = float(building_params.get("floor_thickness",     200.0))
    SILL_HEIGHT     = float(building_params.get("window_sill_height",  900.0))
    WIN_HEIGHT      = float(building_params.get("window_height",      1200.0))
    bboxes = model_results['rois']
    class_ids = model_results['class_ids'] 
    scores = model_results['scores']
    masks = model_results['masks']
    
    objects = []
    door_objects = []
    door_sizes = []
    
    # BIM data arrays
    bim_walls = []
    bim_doors = []
    bim_stairs = []
    bim_slabs = []
    
    for i in range(len(bboxes)):
        bbox = bboxes[i]
        class_id = class_ids[i]
        confidence = float(scores[i])
        mask = masks[:, :, i] if i < masks.shape[2] else None
        
        area = calculateObjectArea(mask) if mask is not None else 0
        center = calculateObjectCenter(bbox)
        width = float(bbox[3] - bbox[1])
        height = float(bbox[2] - bbox[0])
        
        # 1. Door Processing
        if class_id == 3:  
            door_size = max(width, height)
            door_sizes.append(door_size)
            bim_doors.append({
                "id": f"Door_{len(bim_doors)+1}",
                "insertion_point": [float(center["x"]), float(center["y"]), 0.0],
                "width": float(door_size),
                "height": DOOR_HEIGHT,
                "type": "Single-Flush"
            })
            
        # 2. Wall Processing
        elif class_id == 1 and mask is not None:  
            centerline = calculate_centered_straight_centerline(mask, bbox)
            if len(centerline) >= 2:
                start_p = centerline[0]
                end_p = centerline[-1]
                thickness_info = calculate_wall_thickness(mask, centerline)
                avg_thickness = thickness_info.get("average", 10.0)
                bim_walls.append({
                    "id": f"Wall_{len(bim_walls)+1}",
                    "start_point": [float(start_p[0]), float(start_p[1]), 0.0],
                    "end_point": [float(end_p[0]), float(end_p[1]), 0.0],
                    "thickness": float(avg_thickness),
                    "height": WALL_HEIGHT,
                    "type": f"Basic Wall - {int(avg_thickness)}px"
                })

        # 3. Stairs Processing
        elif class_id == 4 and mask is not None:
            stair_data = extract_stair_footprint(mask)
            if stair_data:
                stair_data["id"] = f"Stair_{len(bim_stairs)+1}"
                stair_data["base_level"] = 0.0
                stair_data["top_level"] = WALL_HEIGHT
                bim_stairs.append(stair_data)

        # 4. Slabs (Parking, Balcony, Terrace) Processing
        elif class_id in [5, 6, 7] and mask is not None:
            polygon = extract_slab_polygon(mask)
            if len(polygon) >= 3:
                bim_slabs.append({
                    "id": f"Slab_{len(bim_slabs)+1}",
                    "type": get_extended_class_name(class_id),
                    "thickness": FLOOR_THICKNESS,
                    "elevation": 0.0,
                    "polygon": polygon
                })
        
        # Build base object info
        obj_data = {
            "id": i,
            "type": get_extended_class_name(class_id),
            "confidence": confidence,
            "bbox": {
                "x1": float(bbox[1]), "y1": float(bbox[0]), 
                "x2": float(bbox[3]), "y2": float(bbox[2])
            },
            "dimensions": {"width": width, "height": height, "area": float(area)},
            "center": center
        }
        
        if mask is not None:
            obj_data["mask_analysis"] = encodeMaskSummary(mask)
            obj_data["mask_analysis"]["shape"] = {"height": int(mask.shape[0]), "width": int(mask.shape[1])}
        
        if class_id == 3:
            door_objects.append(obj_data)
        
        objects.append(obj_data)
    
    # Analyze door orientation
    if door_objects:
        door_indices = [i for i, cid in enumerate(class_ids) if cid == 3]
        enhanced_doors = enhancedDoorAnalysis(door_objects, masks, door_indices, image_width, image_height)
        door_index = 0
        for i, obj in enumerate(objects):
            if obj["type"] == "door":
                objects[i] = enhanced_doors[door_index]
                door_index += 1
    
    average_door_size = sum(door_sizes) / len(door_sizes) if door_sizes else 0
    object_counts = {}
    for obj in objects:
        obj_type = obj["type"]
        object_counts[obj_type] = object_counts.get(obj_type, 0) + 1
    
    enhanced_json = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "image_dimensions": {"width": image_width, "height": image_height},
            "total_objects_detected": len(objects),
            "object_counts": object_counts
        },
        "bim_data": {
            "description": "Geometric vector data ready for Revit/BIM modeling",
            "walls": bim_walls,
            "doors": bim_doors,
            "stairs": bim_stairs,
            "slabs": bim_slabs
        },
        "objects": objects,
        "statistics": {
            "average_door_size": float(average_door_size),
            "total_area_detected": sum(obj["dimensions"]["area"] for obj in objects),
            "confidence_scores": {
                "min": float(min(scores)) if len(scores) > 0 else 0,
                "max": float(max(scores)) if len(scores) > 0 else 0,
                "average": float(numpy.mean(scores)) if len(scores) > 0 else 0
            }
        },
        "legacy_format": {
            "Width": image_width,
            "Height": image_height,
            "averageDoor": float(average_door_size),
            "classes": [{"name": get_extended_class_name(cid)} for cid in class_ids],
            "points": [
                {"x1": float(b[1]), "y1": float(b[0]), "x2": float(b[3]), "y2": float(b[2])}
                for b in bboxes
            ]
        }
    }
    
    return enhanced_json