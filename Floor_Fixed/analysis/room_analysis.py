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

from utils.polygon_geometry import polygon_perimeter_m

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Polygon simplification constant
# ─────────────────────────────────────────────────────────────────────────────
# Douglas-Peucker epsilon as a fraction of contour perimeter.
# This controls how aggressively room contours are simplified before being
# sent to Revit / IFC export. Higher = fewer vertices, less faithful.
#
# 0.02 (2% of perimeter) is the empirical sweet spot for rooms:
#   - Rectangular rooms collapse to 4 vertices (ideal)
#   - L-shaped and irregular rooms keep 6-12 vertices (Revit-friendly)
#   - Curved walls retain enough vertices to be visually recognizable
#
# If your output has too many vertices for Revit to handle gracefully, raise
# this to 0.025 or 0.03. If room corners look "rounded off" or chamfered in
# Revit, lower it to 0.015 (which matches slab_analysis.py).
#
# This is intentionally a module-level constant, not a config setting — it is
# a geometry-math knob, not a runtime/deployment concern. Tune in code with a
# fast feedback loop, not via environment variables.
ROOM_SIMPLIFICATION_EPSILON_FACTOR = 0.02


# ─────────────────────────────────────────────────────────────────────────────
# Watershed refinement constants
# ─────────────────────────────────────────────────────────────────────────────
# When two rooms are connected by an open doorway, the flood-fill step merges
# them into a single connected component. We detect this case by counting how
# many OCR text insertion points (room labels) fall inside each component:
# if two or more labels are inside, the component contains two or more rooms.
#
# In that case we run watershed segmentation using the OCR points as markers,
# which splits the merged component along the natural geometric boundary
# (typically the doorway).
#
# Without OCR (or with only one label per component), this refinement is a
# no-op and the algorithm behaves exactly like the old flood-fill-only path.
#
# WATERSHED_MIN_MARKERS_PER_COMPONENT controls the trigger threshold.
# 2 is the natural value: 1 marker means one room (no split needed), 2+ means
# at least one doorway-merge to undo. Raising this disables more refinements.
WATERSHED_MIN_MARKERS_PER_COMPONENT = 2

# When a watershed sub-component's pixel area is below this fraction of the
# parent component, it's discarded as a sliver/artifact. 0.05 = 5% — small
# enough to keep tiny utility rooms (closets), large enough to discard
# segmentation noise.
WATERSHED_MIN_SUBCOMPONENT_FRACTION = 0.05


# ─────────────────────────────────────────────────────────────────────────────
# Artifact filter
# ─────────────────────────────────────────────────────────────────────────────
# After Douglas-Peucker simplification, rooms whose final mm-polygon area is
# below this threshold are dropped as detection artifacts. Typical sources of
# tiny "rooms" that should be filtered:
#   - Wall-mask noise creating false interior gaps (1-2 px in size)
#   - Open doorways briefly considered as "rooms" by the labeller
#   - Watershed slivers that survived the relative threshold but are still
#     too small to be a real room (e.g., 0.3 m² between two adjacent labels)
#
# 0.5 m² (5400 cm²) is a deliberate ceiling: smaller than any real Iranian
# residential room (the smallest plausible bathroom is ~1.5 m²) but larger
# than typical noise. Raise this if your output has spurious tiny rooms;
# lower it if real small spaces (storage closets, equipment chases) are
# being dropped.
#
# This is the FINAL filter — applied AFTER:
#   - Connected-components min_area_px filter (geometric pixel area)
#   - Douglas-Peucker simplification (may reduce area further)
# It's the only filter that sees the actual mm² that ships to the client.
MIN_ROOM_AREA_M2 = 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Watershed refinement helper
# ─────────────────────────────────────────────────────────────────────────────

def _refine_with_watershed(labeled: np.ndarray,
                            wall_uint8: np.ndarray,
                            num_labels: int,
                            ocr_points_px: list) -> np.ndarray:
    """
    Subdivide connected components that contain 2+ OCR markers using watershed.

    The flood-fill stage merges rooms that share an open doorway into a single
    connected component. When two or more OCR room labels fall inside the same
    component, that's strong evidence two rooms have been merged. Watershed
    using the OCR points as markers re-splits them along the natural
    geometric boundary.

    Parameters
    ----------
    labeled : np.ndarray (H, W) int32
        Labelled image from cv2.connectedComponentsWithStats. 0 = background,
        1..num_labels-1 = components.
    wall_uint8 : np.ndarray (H, W) uint8
        Binary wall mask (0 or 255) — used to compute the distance transform
        that watershed flows over.
    num_labels : int
        Number of labels including background.
    ocr_points_px : list of (x_px, y_px) tuples
        OCR insertion points in pixel coordinates.

    Returns
    -------
    np.ndarray (H, W) int32
        Refined label map. Same shape as `labeled`. If watershed fails or no
        refinement is needed, returns the input `labeled` array unchanged.

    Safety
    ------
    Wrapped end-to-end in try/except. Any failure returns the original labels —
    watershed refinement must NEVER be allowed to make the result worse than
    the baseline flood-fill.
    """
    try:
        if not ocr_points_px or num_labels <= 1:
            return labeled  # no markers or no components — nothing to refine

        h, w = labeled.shape
        refined = labeled.copy()
        next_label = int(refined.max()) + 1   # new IDs for subdivided rooms

        # ── Compute the watershed surface ONCE before the loop ──────────────
        # The surface only depends on wall_uint8, not on any specific
        # component, so there's no reason to recompute it inside the loop.
        # Empirical finding: the DILATED wall mask works much better than
        # the raw wall mask, the gradient, or the distance transform.
        # Raw walls are too thin (1-10 px), causing watershed to stop
        # flowing inside rooms prematurely. The dilated mask (the same
        # one used by flood-fill) gives walls enough "presence" to act
        # as proper watershed ridges, so rooms fill completely and the
        # split lands at the geometric midpoint (doorway).
        kernel_ws = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        surface = cv2.dilate(wall_uint8, kernel_ws, iterations=2)
        surface_3ch = cv2.merge([surface, surface, surface])

        # ── For each component, count how many OCR points fall inside ───────
        for comp_id in range(1, num_labels):
            comp_mask = (labeled == comp_id)
            if not comp_mask.any():
                continue

            # Filter OCR points to those inside this component
            points_in_comp = []
            for (px, py) in ocr_points_px:
                ix, iy = int(round(px)), int(round(py))
                if 0 <= iy < h and 0 <= ix < w and comp_mask[iy, ix]:
                    points_in_comp.append((ix, iy))

            if len(points_in_comp) < WATERSHED_MIN_MARKERS_PER_COMPONENT:
                continue   # 0 or 1 marker → leave component alone

            # ── Build markers image for this component ──────────────────────
            # markers[i, j] meaning:
            #   0 = "unknown" (watershed will fill)
            #   1 = background outside the component
            #   2..N = seed for each OCR point inside the component
            markers = np.zeros((h, w), dtype=np.int32)
            markers[~comp_mask] = 1
            for idx, (mx, my) in enumerate(points_in_comp, start=2):
                # Draw a small filled circle at each marker so watershed has
                # a strong seed even if the point sits near a wall pixel.
                cv2.circle(markers, (mx, my), radius=3, color=idx, thickness=-1)

            # ── Run watershed ──────────────────────────────────────────────
            ws_result = markers.copy()
            cv2.watershed(surface_3ch, ws_result)
            # ws_result has -1 at boundaries between regions

            # ── Replace this component's pixels with new sub-IDs ───────────
            # Each marker label (2..N) becomes a new global sub-component ID.
            #
            # CRITICAL: clear the parent component FIRST before writing sub-
            # component IDs. Otherwise, watershed boundary pixels (value -1,
            # forming a 1-px line between regions) and pixels assigned to
            # background by watershed would retain the parent's ID from the
            # initial copy, effectively "leaking" them into the first sub.
            parent_area = int(comp_mask.sum())
            min_sub_area = max(1, int(parent_area * WATERSHED_MIN_SUBCOMPONENT_FRACTION))

            # First, collect the sub-masks (without writing yet)
            sub_masks = []
            for marker_idx in range(2, 2 + len(points_in_comp)):
                sub_mask = (ws_result == marker_idx) & comp_mask
                if int(sub_mask.sum()) >= min_sub_area:
                    sub_masks.append(sub_mask)

            if len(sub_masks) >= 2:
                # Real split — clear the parent area first, then write each sub
                refined[comp_mask] = 0
                for i, sub_mask in enumerate(sub_masks):
                    if i == 0:
                        refined[sub_mask] = comp_id          # keep parent's ID
                    else:
                        refined[sub_mask] = next_label
                        next_label += 1
            # If fewer than 2 valid subs (single marker dominated, or markers
            # too close, or sliver-discard left only one), leave the parent
            # component untouched — preserves baseline behavior.

        return refined

    except Exception as exc:
        logger.warning(
            "Watershed refinement failed (%s) — falling back to flood-fill result",
            exc, exc_info=True
        )
        return labeled


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
    4b. (Optional) Watershed refinement: if 2+ OCR labels fall in one
        component, that component is likely two rooms merged through an
        open doorway. Watershed using the OCR points as markers splits
        them along the natural geometric boundary. No-op if no OCR.
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
          "perimeter_m":  14.1,
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

    # ── 4b. Optional watershed refinement ────────────────────────────────────
    # If multiple OCR markers fall inside one component, that component likely
    # contains 2+ rooms merged via an open doorway. Watershed splits them.
    # If no OCR is available (space_names is None or empty), this is a no-op
    # and the algorithm behaves exactly like the previous flood-fill-only path.
    if space_names:
        ocr_points_px = []
        for sp in space_names:
            ip = sp.get("insertion_point")
            if ip is None or len(ip) < 2:
                continue
            try:
                ocr_points_px.append((float(ip[0]), float(ip[1])))
            except (TypeError, ValueError):
                continue   # malformed OCR entry — skip silently

        if ocr_points_px:
            refined_labels = _refine_with_watershed(
                labeled, wall_uint8, num_labels, ocr_points_px
            )

            # If refinement produced new label IDs, rebuild stats and centroids
            # per-label directly — DO NOT re-run connectedComponentsWithStats
            # on a binary mask, because watershed split components have no
            # wall pixels between them (only label-ID boundaries), so a binary
            # mask would merge them back. Instead, iterate over the unique
            # label IDs from the refined map.
            unique_refined = sorted(int(x) for x in np.unique(refined_labels) if x != 0)
            if len(unique_refined) > (num_labels - 1):
                logger.info(
                    "Watershed refinement: %d component(s) → %d (split via OCR markers)",
                    num_labels - 1, len(unique_refined)
                )

                # Build a contiguous label space (1..N) for the rest of the
                # algorithm. Compute stats and centroids per label.
                new_num_labels = len(unique_refined) + 1   # +1 for background (0)
                new_labeled    = np.zeros_like(refined_labels)
                new_stats      = np.zeros((new_num_labels, 5), dtype=np.int32)
                new_centroids  = np.zeros((new_num_labels, 2), dtype=np.float64)

                for new_id, old_id in enumerate(unique_refined, start=1):
                    comp_mask = (refined_labels == old_id)
                    new_labeled[comp_mask] = new_id

                    ys, xs = np.where(comp_mask)
                    if len(xs) == 0:
                        continue
                    x_min, x_max = int(xs.min()), int(xs.max())
                    y_min, y_max = int(ys.min()), int(ys.max())
                    area_px = int(len(xs))

                    new_stats[new_id, cv2.CC_STAT_LEFT]   = x_min
                    new_stats[new_id, cv2.CC_STAT_TOP]    = y_min
                    new_stats[new_id, cv2.CC_STAT_WIDTH]  = x_max - x_min + 1
                    new_stats[new_id, cv2.CC_STAT_HEIGHT] = y_max - y_min + 1
                    new_stats[new_id, cv2.CC_STAT_AREA]   = area_px
                    new_centroids[new_id, 0] = float(xs.mean())
                    new_centroids[new_id, 1] = float(ys.mean())

                num_labels, labeled, stats, centroids = (
                    new_num_labels, new_labeled, new_stats, new_centroids
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

        # Simplification factor: see ROOM_SIMPLIFICATION_EPSILON_FACTOR docs
        # at the top of this module for tuning guidance.
        epsilon = ROOM_SIMPLIFICATION_EPSILON_FACTOR * perimeter
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
        perimeter_m  = polygon_perimeter_m(polygon_mm)         # m

        # Final-area artifact filter (see MIN_ROOM_AREA_M2 docs at top of file).
        # Drops rooms whose simplified polygon is too small to be real.
        if area_m2 < MIN_ROOM_AREA_M2:
            logger.debug(
                "Dropping room artifact: area_m2=%.3f < threshold %.2f",
                area_m2, MIN_ROOM_AREA_M2
            )
            continue

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
            "perimeter_m": round(perimeter_m, 2),
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
        if ip is None or len(ip) < 2:
            continue
        try:
            ix, iy = int(round(float(ip[0]))), int(round(float(ip[1])))
        except (TypeError, ValueError):
            continue   # non-numeric insertion_point — skip silently
        if 0 <= iy < h and 0 <= ix < w:
            if room_mask_uint8[iy, ix] > 0:
                return space

    # Pass 2 (fallback): return the OCR result whose insertion_point is
    # closest to the room centroid (for small rooms where the text spills out)
    best       = None
    best_dist2 = float("inf")
    for space in space_names:
        ip = space.get("insertion_point")
        if ip is None or len(ip) < 2:
            continue
        try:
            ipx, ipy = float(ip[0]), float(ip[1])
        except (TypeError, ValueError):
            continue   # non-numeric insertion_point — skip silently
        dx   = ipx - cx_px
        dy   = ipy - cy_px
        dist = dx * dx + dy * dy
        if dist < best_dist2:
            best_dist2 = dist
            best       = space

    # Only accept the fallback if it is reasonably close (within 200 px)
    if best is not None and best_dist2 ** 0.5 <= 200.0:
        return best

    return None
