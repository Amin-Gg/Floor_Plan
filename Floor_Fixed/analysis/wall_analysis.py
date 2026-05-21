# Wall analysis module for floor plan processing
import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt
from utils.geometry import find_nearest_valid_point
from utils.conversions import pixels_to_mm, convert_thickness_to_mm, convert_centerline_to_mm, convert_bbox_to_mm
from analysis.junction_analysis import extract_centerline_coords, extract_centerline_coords_with_validation

def calculate_wall_thickness(wall_mask, centerline_coords):
	"""Calculate wall thickness along the centerline"""
	if not centerline_coords or len(centerline_coords) < 2:
		return {"average": 0, "min": 0, "max": 0, "profile": []}
	
	# Calculate distance transform
	distance_map = distance_transform_edt(wall_mask)
	
	thickness_values = []
	for coord in centerline_coords:
		x, y = coord
		if 0 <= y < distance_map.shape[0] and 0 <= x < distance_map.shape[1]:
			# Thickness is approximately 2 * distance to edge
			thickness = distance_map[y, x] * 2
			thickness_values.append(float(thickness))
	
	if not thickness_values:
		return {"average": 0, "min": 0, "max": 0, "profile": []}
	
	return {
		"average": float(np.mean(thickness_values)),
		"min": float(np.min(thickness_values)),
		"max": float(np.max(thickness_values)),
		"profile": thickness_values
	}

def validate_centerline_in_walls(centerline_coords, wall_mask):
    """Additional validation to ensure centerline stays within wall boundaries"""
    if not centerline_coords or len(centerline_coords) < 2:
        return centerline_coords
    
    validated_coords = []
    for coord in centerline_coords:
        x, y = coord
        # Check if point is within image bounds and in wall area
        if (0 <= y < wall_mask.shape[0] and 0 <= x < wall_mask.shape[1] and 
    bool(wall_mask[y, x])):
            validated_coords.append(coord)
        else:
            # Try to find a nearby valid point
            nearest = find_nearest_valid_point(x, y, wall_mask, max_search_radius=3)
            if nearest is not None:
                validated_coords.append([nearest[1], nearest[0]])  # Convert back to [x, y]
    
    return validated_coords if len(validated_coords) >= 2 else centerline_coords

def calculate_wall_length(centerline_coords):
	"""Calculate total wall length from centerline coordinates"""
	if len(centerline_coords) < 2:
		return 0.0
	
	total_length = 0.0
	for i in range(1, len(centerline_coords)):
		x1, y1 = centerline_coords[i-1]
		x2, y2 = centerline_coords[i]
		segment_length = ((x2-x1)**2 + (y2-y1)**2)**0.5
		total_length += segment_length
	
	return float(total_length)

def calculate_wall_orientation(centerline_coords):
	"""Calculate wall orientation angle in degrees"""
	if len(centerline_coords) < 2:
		return 0.0
	
	# Use first and last points for overall orientation
	start_point = centerline_coords[0]
	end_point = centerline_coords[-1]
	
	dx = end_point[0] - start_point[0]
	dy = end_point[1] - start_point[1]
	
	# Calculate angle in degrees
	angle = np.arctan2(dy, dx) * 180 / np.pi
	
	# Normalize to 0-180 degrees (walls don't have direction)
	if angle < 0:
		angle += 180
	if angle > 180:
		angle -= 180
	
	return float(angle)

def find_wall_connections(wall_segments, junctions, tolerance=10):
	"""Find which walls connect at which junctions"""
	connections = {}
	
	for i, segment in enumerate(wall_segments):
		wall_id = f"W{i+1}"
		connections[wall_id] = {"start_junction": None, "end_junction": None}
		
		if len(segment) < 2:
			continue
		
		# Check start and end points of wall
		start_point = [segment[0][1], segment[0][0]]  # Convert y,x to x,y
		end_point = [segment[-1][1], segment[-1][0]]
		
		# Find closest junctions to start and end points
		for j, junction in enumerate(junctions):
			junction_id = f"J{j+1}"
			
			# Distance to start point
			start_dist = ((start_point[0] - junction[0])**2 + (start_point[1] - junction[1])**2)**0.5
			if start_dist <= tolerance and not connections[wall_id]["start_junction"]:
				connections[wall_id]["start_junction"] = junction_id
			
			# Distance to end point  
			end_dist = ((end_point[0] - junction[0])**2 + (end_point[1] - junction[1])**2)**0.5
			if end_dist <= tolerance and not connections[wall_id]["end_junction"]:
				connections[wall_id]["end_junction"] = junction_id
	
	return connections

def analyze_junction_types(junctions, wall_connections):
	"""Analyze the type of each junction based on connected walls"""
	junction_analysis = []
	
	for i, junction in enumerate(junctions):
		junction_id = f"J{i+1}"
		
		# Count walls connected to this junction
		connected_walls = []
		for wall_id, connections in wall_connections.items():
			if (connections["start_junction"] == junction_id or 
				connections["end_junction"] == junction_id):
				connected_walls.append(wall_id)
		
		# Determine junction type
		junction_type = "unknown"
		if len(connected_walls) == 2:
			junction_type = "corner"
		elif len(connected_walls) == 3:
			junction_type = "T_junction"
		elif len(connected_walls) == 4:
			junction_type = "cross_junction"
		elif len(connected_walls) > 4:
			junction_type = "complex_junction"
		
		junction_analysis.append({
			"junction_id": junction_id,
			"position": [float(junction[0]), float(junction[1])],
			"connected_walls": connected_walls,
			"junction_type": junction_type,
			"wall_count": len(connected_walls)
		})
	
	return junction_analysis

def snap_centerlines_to_junctions(wall_parameters: list,
                                   snap_tolerance_mm: float = 50.0) -> list:
    """
    Snap wall endpoint coordinates to nearby junction points so that walls
    meet precisely in 3D modeling software.

    When cv2.fitLine computes a wall centerline it terminates at the bounding
    box of the wall mask — not at the true geometric intersection with adjacent
    walls. This leaves gaps of a few pixels at every corner. In Revit and
    ArchiCAD, un-snapped walls create open corners that break:
        - Room area computation
        - Plan export (PDF/DWG)
        - IFC solid geometry rendering

    Algorithm
    ----------
    For every wall, collect all junction points within snap_tolerance_mm of
    either the start or end point of the centerline.  If found, replace the
    endpoint with the exact junction coordinate.  The junction coordinates are
    the authoritative intersection points computed by junction_analysis.py.

    Parameters
    ----------
    wall_parameters    : list of wall dicts (output of extract_wall_parameters)
                         Each dict must have "centerline": [[x,y], ...] in mm
    snap_tolerance_mm  : maximum distance in mm to snap an endpoint to a junction.
                         Default 50 mm — larger than any fitting error, smaller
                         than the shortest wall segment.

    Returns
    -------
    list — same structure as input, with endpoint coordinates snapped in-place.
    """
    # Collect all junction points from wall connection metadata
    junction_points: dict = {}   # junction_id → [x_mm, y_mm]

    for wall in wall_parameters:
        connections = wall.get("connections", {})
        # The connections dict has start_junction and end_junction IDs but not
        # coordinates. We reconstruct junction positions from the centerline
        # endpoints themselves — if two walls share a junction ID, their
        # respective endpoints should be at the same coordinate.
        # Strategy: build a map of junction_id → list of endpoint coords,
        # then average them to get the consensus junction position.
        for side in ("start_junction", "end_junction"):
            jid = connections.get(side)
            if not jid:
                continue
            cl = wall.get("centerline", [])
            if len(cl) < 2:
                continue
            pt = cl[0] if side == "start_junction" else cl[-1]
            junction_points.setdefault(jid, []).append(pt)

    # Compute consensus position for each junction (average of all contributors)
    junction_consensus: dict = {}
    for jid, pts in junction_points.items():
        if pts:
            junction_consensus[jid] = [
                sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts),
            ]

    # Snap each wall endpoint to its junction consensus position
    snapped_count = 0
    for wall in wall_parameters:
        cl = wall.get("centerline", [])
        if len(cl) < 2:
            continue

        connections = wall.get("connections", {})

        for side, idx in (("start_junction", 0), ("end_junction", -1)):
            jid = connections.get(side)
            if not jid or jid not in junction_consensus:
                continue

            jx, jy = junction_consensus[jid]
            ex, ey = cl[idx][0], cl[idx][1]
            dist   = ((jx - ex) ** 2 + (jy - ey) ** 2) ** 0.5

            if dist <= snap_tolerance_mm:
                cl[idx] = [jx, jy]
                snapped_count += 1

    import logging
    logging.getLogger(__name__).info(
        "snap_centerlines_to_junctions: snapped %d endpoints (tolerance=%.0f mm)",
        snapped_count, snap_tolerance_mm
    )
    return wall_parameters


def extract_wall_parameters(segments, wall_mask, junctions, scale_factor_mm_per_pixel=1.0):
    """Extract comprehensive parameters for each wall segment (output only mm fields)"""
    wall_parameters = []
    wall_connections = find_wall_connections(segments, junctions)
    for i, segment in enumerate(segments):
        wall_id = f"W{i+1}"
        centerline = extract_centerline_coords(segment)
        length_px = calculate_wall_length(centerline)
        thickness_px = calculate_wall_thickness(wall_mask, centerline)
        orientation = calculate_wall_orientation(centerline)
        if len(segment) > 0:
            min_y, min_x = np.min(segment, axis=0)
            max_y, max_x = np.max(segment, axis=0)
            bbox_px = {
                "x1": float(min_x), "y1": float(min_y),
                "x2": float(max_x), "y2": float(max_y)
            }
        else:
            bbox_px = {"x1": 0, "y1": 0, "x2": 0, "y2": 0}
        length_mm = pixels_to_mm(length_px, scale_factor_mm_per_pixel)
        thickness_mm = convert_thickness_to_mm(thickness_px, scale_factor_mm_per_pixel)
        centerline_mm = convert_centerline_to_mm(centerline, scale_factor_mm_per_pixel)
        bbox_mm = convert_bbox_to_mm(bbox_px, scale_factor_mm_per_pixel)
        wall_params = {
            "wall_id": wall_id,
            "centerline": centerline_mm,
            "length": length_mm,
            "thickness": thickness_mm,
            "orientation_degrees": orientation,
            "bbox": bbox_mm,
            "connections": wall_connections.get(wall_id, {"start_junction": None, "end_junction": None}),
            "segment_area": float(len(segment))
        }
        wall_parameters.append(wall_params)

    # Snap wall endpoints to junction consensus positions so corners close
    # cleanly in Revit, ArchiCAD, and other 3D modeling software.
    wall_parameters = snap_centerlines_to_junctions(wall_parameters)

    return wall_parameters

def extract_wall_parameters_with_regions(all_wall_segments, wall_mask, junctions, scale_factor_mm_per_pixel=1.0):
    """Extract comprehensive parameters for each wall segment with region prefixes (output only mm fields)"""
    wall_parameters = []
    wall_connections = find_wall_connections([seg for seg, _ in all_wall_segments], junctions)
    for i, (segment, region_prefix) in enumerate(all_wall_segments):
        wall_id = f"{region_prefix}W{i+1}"
        centerline = extract_centerline_coords_with_validation(segment, wall_mask)
        length_px = calculate_wall_length(centerline)
        thickness_px = calculate_wall_thickness(wall_mask, centerline)
        orientation = calculate_wall_orientation(centerline)
        if len(segment) > 0:
            min_y, min_x = np.min(segment, axis=0)
            max_y, max_x = np.max(segment, axis=0)
            bbox_px = {
                "x1": float(min_x), "y1": float(min_y),
                "x2": float(max_x), "y2": float(max_y)
            }
        else:
            bbox_px = {"x1": 0, "y1": 0, "x2": 0, "y2": 0}
        length_mm = pixels_to_mm(length_px, scale_factor_mm_per_pixel)
        thickness_mm = convert_thickness_to_mm(thickness_px, scale_factor_mm_per_pixel)
        centerline_mm = convert_centerline_to_mm(centerline, scale_factor_mm_per_pixel)
        bbox_mm = convert_bbox_to_mm(bbox_px, scale_factor_mm_per_pixel)
        wall_params = {
            "wall_id": wall_id,
            "centerline": centerline_mm,
            "length": length_mm,
            "thickness": thickness_mm,
            "orientation_degrees": orientation,
            "bbox": bbox_mm,
            "connections": wall_connections.get(wall_id, {"start_junction": None, "end_junction": None}),
            "segment_area": float(len(segment))
        }
        wall_parameters.append(wall_params)

    # Snap wall endpoints to junction consensus positions — identical to the
    # snap applied in extract_wall_parameters. Without this call, walls
    # processed through the region-based path have open corners in Revit.
    wall_parameters = snap_centerlines_to_junctions(wall_parameters)

    return wall_parameters



def generate_wall_insights(wall_parameters, junction_analysis):
	"""Generate architectural insights about wall layout"""
	insights = []
	
	if not wall_parameters:
		return ["No walls detected for analysis"]
	
	# Analyze wall thickness consistency
	thicknesses = [w["thickness"]["average"] for w in wall_parameters if w["thickness"]["average"] > 0]
	if thicknesses:
		thickness_std = np.std(thicknesses)
		if thickness_std < 2.0:
			insights.append("Consistent wall thickness throughout floor plan")
		else:
			insights.append("Variable wall thickness detected - may indicate different wall types")
	
	# Analyze wall length distribution
	lengths = [w["length_px"] for w in wall_parameters]
	if lengths:
		long_walls = len([l for l in lengths if l > np.mean(lengths) * 1.5])
		if long_walls > 0:
			insights.append(f"Found {long_walls} notably long walls - potential load-bearing structures")
	
	# Analyze junction complexity
	complex_junctions = [j for j in junction_analysis if j["wall_count"] > 3]
	if complex_junctions:
		insights.append(f"Found {len(complex_junctions)} complex junctions with 4+ walls")
	
	# Check for isolated walls
	isolated_walls = [w for w in wall_parameters if 
					 w["connections"]["start_junction"] is None and 
					 w["connections"]["end_junction"] is None]
	if isolated_walls:
		insights.append(f"Found {len(isolated_walls)} isolated wall segments")
	
	# Analyze wall orientation patterns
	orientations = [w["orientation_degrees"] for w in wall_parameters]
	horizontal_walls = len([o for o in orientations if o <= 30 or o >= 150])
	vertical_walls = len([o for o in orientations if 60 <= o <= 120])
	
	if horizontal_walls > vertical_walls * 1.5:
		insights.append("Predominantly horizontal wall layout")
	elif vertical_walls > horizontal_walls * 1.5:
		insights.append("Predominantly vertical wall layout")
	else:
		insights.append("Balanced horizontal and vertical wall layout")
	
	return insights


def identify_exterior_walls(wall_parameters, image_width, image_height, scale_factor_mm_per_pixel):
    """Identify walls that form the exterior boundary of the floor plan"""
    exterior_walls = []
    interior_walls = []

    # Convert image dimensions to mm for boundary analysis
    image_width_mm = pixels_to_mm(image_width, scale_factor_mm_per_pixel)
    image_height_mm = pixels_to_mm(image_height, scale_factor_mm_per_pixel)

    # Define boundary margins (walls within 3% of image edges are likely exterior)
    boundary_margin = 0.03  # 3% of image dimensions (more strict)
    x_margin = image_width_mm * boundary_margin
    y_margin = image_height_mm * boundary_margin

    for wall in wall_parameters:
        bbox = wall.get("bbox", {})
        x1, y1, x2, y2 = bbox.get("x1", 0), bbox.get("y1", 0), bbox.get("x2", 0), bbox.get("y2", 0)
        
        # Check if wall is near any image boundary
        is_near_left = x1 <= x_margin
        is_near_right = x2 >= (image_width_mm - x_margin)
        is_near_top = y1 <= y_margin
        is_near_bottom = y2 >= (image_height_mm - y_margin)
        
        # Additional criteria: walls with fewer connections are more likely to be exterior
        connections = wall.get("connections", {})
        start_connected = connections.get("start_junction") is not None
        end_connected = connections.get("end_junction") is not None
        connection_count = sum([start_connected, end_connected])
        
        # Determine if this is likely an exterior wall
        is_exterior = False
        exterior_reasons = []
        
        # Criterion 1: Near image boundaries (more strict)
        if is_near_left or is_near_right or is_near_top or is_near_bottom:
            is_exterior = True
            if is_near_left:
                exterior_reasons.append("left_boundary")
            if is_near_right:
                exterior_reasons.append("right_boundary")
            if is_near_top:
                exterior_reasons.append("top_boundary")
            if is_near_bottom:
                exterior_reasons.append("bottom_boundary")
        
        # Criterion 2: Poorly connected walls (likely exterior) - more strict
        if connection_count == 0 and not is_exterior:  # Only unconnected walls
            is_exterior = True
            exterior_reasons.append("unconnected")
        
        # Criterion 3: Long walls near boundaries (likely perimeter walls)
        wall_length = wall.get("length", 0)
        if wall_length > 150 and (is_near_left or is_near_right or is_near_top or is_near_bottom):  # Increased threshold
            is_exterior = True
            exterior_reasons.append("long_boundary_wall")
        
        # Criterion 4: Walls that span a significant portion of the image edge
        if is_near_left or is_near_right:
            wall_span = abs(y2 - y1)
            if wall_span > image_height_mm * 0.3:  # Spans 30% of image height
                is_exterior = True
                exterior_reasons.append("spans_vertical_edge")
        
        if is_near_top or is_near_bottom:
            wall_span = abs(x2 - x1)
            if wall_span > image_width_mm * 0.3:  # Spans 30% of image width
                is_exterior = True
                exterior_reasons.append("spans_horizontal_edge")
        
        if is_exterior:
            exterior_wall_data = wall.copy()
            exterior_wall_data["exterior_reasons"] = exterior_reasons
            exterior_walls.append(exterior_wall_data)
        else:
            interior_walls.append(wall)

    return exterior_walls, interior_walls


def calculate_perimeter_dimensions(exterior_walls):
	"""Calculate perimeter dimensions from exterior walls"""
	if not exterior_walls:
		return {
			"total_perimeter_length": 0,
			"exterior_wall_count": 0,
			"perimeter_area": 0,
			"boundary_coverage": {
				"left": 0, "right": 0, "top": 0, "bottom": 0
			}
		}
	
	total_perimeter_length = sum(wall.get("length", 0) for wall in exterior_walls)
	exterior_wall_count = len(exterior_walls)
	
	# Calculate approximate perimeter area (assuming rectangular shape)
	# This is a rough estimate based on the longest walls in each direction
	wall_lengths = [wall.get("length", 0) for wall in exterior_walls]
	if len(wall_lengths) >= 4:
		# Sort by length and assume the 4 longest walls form the rectangle
		sorted_lengths = sorted(wall_lengths, reverse=True)
		width_estimate = sorted_lengths[0]  # Longest wall
		height_estimate = sorted_lengths[1]  # Second longest wall
		perimeter_area = width_estimate * height_estimate
	else:
		perimeter_area = 0
	
	# Analyze boundary coverage
	boundary_coverage = {"left": 0, "right": 0, "top": 0, "bottom": 0}
	for wall in exterior_walls:
		reasons = wall.get("exterior_reasons", [])
		for reason in reasons:
			if reason in boundary_coverage:
				boundary_coverage[reason] += 1
	
	return {
		"total_perimeter_length": total_perimeter_length,
		"exterior_wall_count": exterior_wall_count,
		"perimeter_area": perimeter_area,
		"boundary_coverage": boundary_coverage,
		"average_exterior_wall_length": total_perimeter_length / exterior_wall_count if exterior_wall_count > 0 else 0
	}

def calculate_centered_straight_centerline(wall_mask, bbox=None):
    """
    Calculate a perfectly straight centerline for ANY angle (horizontal, vertical, or diagonal).
    Uses geometric line fitting (cv2.fitLine) to create Revit-ready vectors.
    """
    if np.sum(wall_mask) < 10:
        return []
    
    # 1. Calculate distance transform to find medial axis (ضخامت دیوار)
    distance_map = distance_transform_edt(wall_mask)
    
    # 2. Find the ridge points (پیکسل‌های مرکزی)
    threshold = np.percentile(distance_map[distance_map > 0], 70)
    ridge_points = np.where(distance_map >= threshold)
    
    if len(ridge_points[0]) < 2:
        return []
        
    # 3. Convert to (x, y) coordinates
    points = np.column_stack((ridge_points[1], ridge_points[0])).astype(np.float32)
    
    # 4. Mathematical Vectorization (تشخیص زاویه دقیق دیوار)
    # cv2.fitLine finds the perfect mathematical line passing through the points
    [vx, vy, x, y] = cv2.fitLine(points, cv2.DIST_L2, 0, 0.01, 0.01)
    vx, vy = float(vx[0]), float(vy[0])
    x0, y0 = float(x[0]), float(y[0])
    
    # 5. Project points onto the mathematical line to find exact Start and End points
    t_values = []
    for p in points:
        px, py = p[0], p[1]
        t = (px - x0) * vx + (py - y0) * vy
        t_values.append(t)
        
    t_min, t_max = min(t_values), max(t_values)
    
    # Absolute start and end coordinates (مختصات دقیق برداری)
    start_point = [x0 + t_min * vx, y0 + t_min * vy]
    end_point = [x0 + t_max * vx, y0 + t_max * vy]
    
    # 6. Generate evenly spaced points along this PERFECT line
    # (To maintain compatibility with your length calculation functions)
    num_points = max(3, int(np.sqrt((end_point[0]-start_point[0])**2 + (end_point[1]-start_point[1])**2) / 10))
    straight_x = np.linspace(start_point[0], end_point[0], num_points)
    straight_y = np.linspace(start_point[1], end_point[1], num_points)
    
    centerline = [[x, y] for x, y in zip(straight_x, straight_y)]
    
    # 7. Safety clamp to ensure points stay within image boundaries
    validated_centerline = []
    for point in centerline:
        x_clamp = max(0, min(point[0], wall_mask.shape[1] - 1))
        y_clamp = max(0, min(point[1], wall_mask.shape[0] - 1))
        validated_centerline.append([float(x_clamp), float(y_clamp)])

    return validated_centerline if len(validated_centerline) >= 2 else []