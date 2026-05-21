"""
Room Analysis Module
====================
Extracts closed room polygons, areas, and dimensions from the wall mask.
Also provides host-wall lookup for doors and windows.

Two main public functions:
    extract_room_polygons(combined_wall_mask, scale_factor_mm_per_pixel, space_names)
        → list of room dicts with polygon, area_m2, dimensions, name from OCR

    find_host_wall_id(insertion_point_mm, wall_parameters)
        → wall_id string of the wall closest to a door or window insertion point
"""

import cv2
import logging
import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public: Room Polygon Extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_room_polygons(combined_wall_mask, scale_factor_mm_per_pixel,
                          space_names=None):
    """
    Detect closed room polygons from the combined wall mask.

    Algorithm
    ---------
    1. Dilate the wall mask to close small gaps between wall segments.
    2. Invert the mask → non-wall pixels are potential interior spaces.
    3. Flood-fill from all four image borders → marks the exterior.
    4. Label the remaining connected components → each is one room.
    5. Extract and simplify the contour of each component (Douglas-Peucker).
    6. Convert pixel coordinates to mm, compute area and bounding dimensions.
    7. Match each room centroid to an OCR space name via point-in-mask test.

    Parameters
    ----------
    combined_wall_mask : np.ndarray  (H, W), bool or uint8
        The merged wall binary mask AFTER door/window regions have been removed.
    scale_factor_mm_per_pixel : float
        Conversion factor: 1 pixel = this many millimetres.
    space_names : list of dict, optional
        OCR results.  Each dict must have:
            "insertion_point" : [x_px, y_px]   ← pixel coordinates
            "name"            : str             ← English room name
            "local_name"      : str             ← Farsi room name
            "category"        : str

    Returns
    -------
    list of dict, one per detected room:
        {
          "id":           "Room_1",
          "name":         "Bedroom",
          "local_name":   "اتاق خواب",
          "category":     "Accommodation",
          "polygon":      [[x_mm, y_mm], ...],   # closed (last == first)
          "area_m2":      12.4,
          "dimensions": {
              "length_mm": 3800.0,               # longer side of bounding box
              "width_mm":  3250.0                # shorter side
          },
          "centroid_mm":  [x_mm, y_mm],
          "vertex_count": 4                      # polygon vertices (excl. closing point)
        }
    """
    h, w = combined_wall_mask.shape

    # ── 1. Close small gaps in the wall mask ─────────────────────────────────
    wall_uint8 = (combined_wall_mask > 0).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed_walls = cv2.dilate(wall_uint8, kernel, iterations=2)

    # ── 2. Invert → interior spaces ──────────────────────────────────────────
    non_wall = cv2.bitwise_not(closed_walls)

    # ── 3. Flood-fill from image borders → mark exterior as grey (128) ───────
    flood_filled = non_wall.copy()
    flood_mask   = np.zeros((h + 2, w + 2), np.uint8)   # required by cv2.floodFill

    def _fill(img, x, y):
        if img[y, x] > 0:
            cv2.floodFill(img, flood_mask, (x, y), 128)

    for x in range(w):
        _fill(flood_filled, x, 0)
        _fill(flood_filled, x, h - 1)
    for y in range(h):
        _fill(flood_filled, 0, y)
        _fill(flood_filled, w - 1, y)

    # Interior rooms = non-wall pixels that flood-fill did NOT reach
    interior_mask = np.where(
        (non_wall > 0) & (flood_filled != 128), 255, 0
    ).astype(np.uint8)

    # ── 4. Label connected components ────────────────────────────────────────
    num_labels, labeled, stats, centroids = cv2.connectedComponentsWithStats(
        interior_mask, connectivity=8
    )

    # Minimum room size: 500 mm × 500 mm in pixel area
    min_area_px = max(100, (500.0 / scale_factor_mm_per_pixel) ** 2)

    rooms = []

    for label_id in range(1, num_labels):     # 0 = background, skip it
        area_px = int(stats[label_id, cv2.CC_STAT_AREA])
        if area_px < min_area_px:
            continue

        # ── 5. Contour + Douglas-Peucker simplification ───────────────────
        room_mask = (labeled == label_id).astype(np.uint8) * 255
        contours, _ = cv2.findContours(room_mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour   = max(contours, key=cv2.contourArea)
        perimeter = cv2.arcLength(contour, True)
        if perimeter == 0:
            continue

        # Simplification factor: 2% of perimeter — keeps rectangles as 4 pts,
        # complex shapes as 6-12 pts, which is Revit-friendly.
        epsilon = 0.02 * perimeter
        approx  = cv2.approxPolyDP(contour, epsilon, True)

        if len(approx) < 3:
            continue

        # ── 6. Convert to mm ──────────────────────────────────────────────
        polygon_mm = [
            [float(pt[0][0] * scale_factor_mm_per_pixel),
             float(pt[0][1] * scale_factor_mm_per_pixel)]
            for pt in approx
        ]

        # Ensure polygon is explicitly closed
        if polygon_mm[0] != polygon_mm[-1]:
            polygon_mm.append(polygon_mm[0])

        area_m2      = _shoelace_area_mm(polygon_mm) / 1e6    # mm² → m²
        x_coords     = [p[0] for p in polygon_mm]
        y_coords     = [p[1] for p in polygon_mm]
        bbox_w_mm    = max(x_coords) - min(x_coords)
        bbox_h_mm    = max(y_coords) - min(y_coords)

        cx_px = float(centroids[label_id][0])
        cy_px = float(centroids[label_id][1])

        # ── 7. Match OCR name ─────────────────────────────────────────────
        room_name       = "Room"
        room_local_name = ""
        room_category   = "Unknown"

        if space_names:
            matched = _match_ocr_name(cx_px, cy_px, room_mask, space_names)
            if matched:
                room_name       = matched.get("name",       "Room")
                room_local_name = matched.get("local_name", "")
                room_category   = matched.get("category",   "Unknown")

        rooms.append({
            "id":         f"Room_{len(rooms) + 1}",
            "name":       room_name,
            "local_name": room_local_name,
            "category":   room_category,
            "polygon":    polygon_mm,
            "area_m2":    round(area_m2, 2),
            "dimensions": {
                "length_mm": round(max(bbox_w_mm, bbox_h_mm), 1),
                "width_mm":  round(min(bbox_w_mm, bbox_h_mm), 1)
            },
            "centroid_mm": [
                round(cx_px * scale_factor_mm_per_pixel, 1),
                round(cy_px * scale_factor_mm_per_pixel, 1)
            ],
            "vertex_count": len(polygon_mm) - 1    # exclude closing point
        })

    logger.info(f"Room analysis: {len(rooms)} rooms extracted")
    return rooms


# ─────────────────────────────────────────────────────────────────────────────
# Public: Host Wall Lookup (for Doors and Windows)
# ─────────────────────────────────────────────────────────────────────────────

def find_host_wall_id(insertion_point_mm, wall_parameters):
    """
    Find the wall that hosts a door or window.

    Searches for the wall whose centerline is geometrically closest to the
    given insertion point.  The insertion point is the centre of the door or
    window bounding box, in millimetres.

    Parameters
    ----------
    insertion_point_mm : [x_mm, y_mm]
        Centre of the door or window, in millimetres.
    wall_parameters : list of dict
        Wall parameters as produced by extract_wall_parameters().
        Each dict must have "wall_id" and "centerline" (list of [x,y] in mm).

    Returns
    -------
    str or None
        The wall_id of the closest wall, or None if no wall is found.
    """
    px, py   = float(insertion_point_mm[0]), float(insertion_point_mm[1])
    min_dist = float("inf")
    host_id  = None

    for wall in wall_parameters:
        cl = wall.get("centerline", [])
        if len(cl) < 2:
            continue

        # Walk every segment of the (possibly multi-point) centerline
        for i in range(len(cl) - 1):
            x1, y1 = float(cl[i][0]),     float(cl[i][1])
            x2, y2 = float(cl[i + 1][0]), float(cl[i + 1][1])

            dist = _point_to_segment_distance(px, py, x1, y1, x2, y2)
            if dist < min_dist:
                min_dist = dist
                host_id  = wall["wall_id"]

    # Sanity guard: if the nearest wall is unreasonably far (> 1 000 mm away
    # from the insertion point) something is wrong — don't assign a host.
    if min_dist > 1000.0:
        logger.warning(
            f"find_host_wall_id: nearest wall is {min_dist:.0f} mm away — "
            "no host assigned.  Check scale_factor_mm_per_pixel."
        )
        return None

    return host_id


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _shoelace_area_mm(polygon_mm):
    """
    Compute the signed area of a closed polygon using the shoelace formula.
    Input:  list of [x_mm, y_mm], closed (last point == first point).
    Output: area in mm² (always positive).
    """
    n    = len(polygon_mm)
    area = 0.0
    for i in range(n):
        j     = (i + 1) % n
        area += polygon_mm[i][0] * polygon_mm[j][1]
        area -= polygon_mm[j][0] * polygon_mm[i][1]
    return abs(area) / 2.0


def _point_to_segment_distance(px, py, x1, y1, x2, y2):
    """
    Minimum distance from point (px, py) to the line segment (x1,y1)→(x2,y2).
    Returns the perpendicular distance if the foot lies on the segment,
    otherwise the distance to the nearer endpoint.
    """
    dx = x2 - x1
    dy = y2 - y1
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq < 1e-9:                       # degenerate segment (zero length)
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5

    # Parametric projection of point onto the infinite line
    t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))                   # clamp to [0, 1] → segment

    foot_x = x1 + t * dx
    foot_y = y1 + t * dy

    return ((px - foot_x) ** 2 + (py - foot_y) ** 2) ** 0.5


def _match_ocr_name(cx_px, cy_px, room_mask_uint8, space_names):
    """
    Find the first OCR result whose pixel insertion_point falls inside the
    given room mask.  Falls back to the nearest centroid if none lands inside.

    Parameters
    ----------
    cx_px, cy_px      : float — room centroid in pixel coordinates
    room_mask_uint8   : np.ndarray (H, W) uint8  — 255 inside room, 0 outside
    space_names       : list of OCR result dicts (must have "insertion_point")

    Returns
    -------
    dict or None — the matched space_name entry
    """
    h, w = room_mask_uint8.shape

    # Pass 1: check if OCR point is physically inside this room's mask
    for space in space_names:
        ip = space.get("insertion_point")
        if ip is None:
            continue
        ix, iy = int(round(float(ip[0]))), int(round(float(ip[1])))
        if 0 <= iy < h and 0 <= ix < w:
            if room_mask_uint8[iy, ix] > 0:
                return space

    # Pass 2 (fallback): return the OCR result whose insertion_point is
    # closest to the room centroid (for small rooms where the text spills out)
    best       = None
    best_dist2 = float("inf")
    for space in space_names:
        ip = space.get("insertion_point")
        if ip is None:
            continue
        dx   = float(ip[0]) - cx_px
        dy   = float(ip[1]) - cy_px
        dist = dx * dx + dy * dy
        if dist < best_dist2:
            best_dist2 = dist
            best       = space

    # Only accept the fallback if it is reasonably close (within 200 px)
    if best is not None and best_dist2 ** 0.5 <= 200.0:
        return best

    return None
