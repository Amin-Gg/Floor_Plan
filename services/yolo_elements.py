"""
services/yolo_elements.py
=========================
Convert supplementary YOLO box detections into bim_data element entries.

YOLO gives boxes, not masks, so geometry here is APPROXIMATE (box-derived) and is
flagged `"mask_based": false, "source": "yolo"` so downstream code (and the IFC
exporter) can treat it accordingly. Columns/staircases become rectangular
footprints; railings become a line segment along the box's longer axis.

Merge safety
------------
`mask_covered_classes` lets the caller suppress YOLO elements whose type is
already produced by the (mask-based) Mask R-CNN engine — so if you later train
the .h5 to detect stairs, pass {"Stairs"} and YOLO stairs are dropped in favor
of the mask geometry. With the current 3-class .h5 (wall/window/door), nothing
YOLO contributes overlaps, so the default empty set is correct.
"""

import logging
from typing import Dict, List, FrozenSet

logger = logging.getLogger(__name__)


def _box_to_mm(bbox, s):
    """[y1,x1,y2,x2] px → corner coords + metrics in mm (s = mm per pixel)."""
    y1, x1, y2, x2 = [float(v) for v in bbox]
    X1, Y1, X2, Y2 = x1 * s, y1 * s, x2 * s, y2 * s
    w = abs(X2 - X1)
    h = abs(Y2 - Y1)
    cx = (X1 + X2) / 2.0
    cy = (Y1 + Y2) / 2.0
    footprint = [[X1, Y1], [X2, Y1], [X2, Y2], [X1, Y2], [X1, Y1]]  # closed
    return X1, Y1, X2, Y2, w, h, cx, cy, footprint


def build_yolo_elements(
    detections: List[dict],
    scale_factor_mm_per_pixel: float,
    *,
    wall_height_mm: float = 2800.0,
    railing_height_mm: float = 1000.0,
    mask_covered_classes: FrozenSet[str] = frozenset(),
) -> Dict[str, List[dict]]:
    """
    Returns a dict of bim_data buckets built from YOLO detections:
        {"columns": [...], "railings": [...], "stairs": [...], "curtain_walls": [...]}
    Empty buckets are returned as empty lists.
    """
    s = float(scale_factor_mm_per_pixel)
    out: Dict[str, List[dict]] = {"columns": [], "railings": [], "stairs": [], "curtain_walls": []}

    skipped_covered = 0
    for d in detections:
        etype = d.get("element_type")
        if etype in mask_covered_classes:
            skipped_covered += 1
            continue

        bucket = d.get("bucket", "")
        conf = round(float(d.get("confidence", 0.0)), 4)
        X1, Y1, X2, Y2, w, h, cx, cy, footprint = _box_to_mm(d["bbox"], s)

        if bucket == "columns":
            out["columns"].append({
                "id":               f"Column_{len(out['columns']) + 1}",
                "type":             etype,
                "footprint_polygon": footprint,
                "center_mm":        [round(cx, 1), round(cy, 1)],
                "width_mm":         round(w, 1),
                "depth_mm":         round(h, 1),
                "base_level":       0.0,
                "top_level":        round(wall_height_mm, 1),
                "confidence":       conf,
                "source":           "yolo",
                "mask_based":       False,
            })

        elif bucket == "railings":
            # railing runs along the box's LONGER axis → a line segment
            if w >= h:
                start, end, length = [X1, cy], [X2, cy], w
            else:
                start, end, length = [cx, Y1], [cx, Y2], h
            out["railings"].append({
                "id":          f"Railing_{len(out['railings']) + 1}",
                "type":        etype,
                "start_mm":    [round(start[0], 1), round(start[1], 1)],
                "end_mm":      [round(end[0], 1), round(end[1], 1)],
                "length_mm":   round(length, 1),
                "height_mm":   round(railing_height_mm, 1),
                "base_level":  0.0,
                "confidence":  conf,
                "source":      "yolo",
                "mask_based":  False,
            })

        elif bucket == "stairs":
            out["stairs"].append({
                "id":               f"Stair_{len(out['stairs']) + 1}",
                "footprint_polygon": footprint,
                "center_mm":        [round(cx, 1), round(cy, 1)],
                "width_mm":         round(min(w, h), 1),
                "length_mm":        round(max(w, h), 1),
                "rotation_angle":   0.0,        # unknown from a box; default upright
                "base_level":       0.0,
                "top_level":        round(wall_height_mm, 1),
                "confidence":       conf,
                "source":           "yolo",
                "mask_based":       False,
            })

        elif bucket == "curtain_walls":
            if w >= h:
                start, end, length = [X1, cy], [X2, cy], w
            else:
                start, end, length = [cx, Y1], [cx, Y2], h
            out["curtain_walls"].append({
                "id":          f"CurtainWall_{len(out['curtain_walls']) + 1}",
                "type":        etype,
                "start_mm":    [round(start[0], 1), round(start[1], 1)],
                "end_mm":      [round(end[0], 1), round(end[1], 1)],
                "length_mm":   round(length, 1),
                "height_mm":   round(wall_height_mm, 1),
                "base_level":  0.0,
                "confidence":  conf,
                "source":      "yolo",
                "mask_based":  False,
            })
        else:
            logger.debug("YOLO detection with unknown bucket %r skipped", bucket)

    if skipped_covered:
        logger.info("YOLO elements: skipped %d detection(s) already covered by "
                    "mask-based detection (%s)", skipped_covered,
                    ", ".join(sorted(mask_covered_classes)))
    logger.info("YOLO elements built: %d columns, %d railings, %d stairs, %d curtain_walls",
                len(out["columns"]), len(out["railings"]), len(out["stairs"]), len(out["curtain_walls"]))
    return out
