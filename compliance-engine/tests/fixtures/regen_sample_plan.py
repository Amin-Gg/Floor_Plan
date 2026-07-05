"""
tests/fixtures/regen_sample_plan.py
===================================
Regenerate the round-trip fixtures used by tests/test_ifc_roundtrip_verdicts.py:

    tests/fixtures/sample_plan_bim.json   — source bim_data (orchestrator BIM + centroids)
    tests/fixtures/sample_plan.ifc        — that plan, exported by Step 1's exporter

This is the ONLY step that needs the Step-1 repo (its enriched ifc_exporter).
The tests themselves do not import it. Run only when the exporter or the source
plan changes:

    PYTHONPATH=/path/to/Floor_Plan python tests/fixtures/regen_sample_plan.py

(adjust STEP1_REPO below or set it via the FLOORPLAN_REPO env var).
"""

import json
import os
import sys

STEP1_REPO = os.getenv("FLOORPLAN_REPO", "/home/claude/review/floor_plan_extract/Floor_Plan")


def _rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def _centroid(poly):
    pts = poly[:-1] if poly[0] == poly[-1] else poly
    return [round(sum(p[0] for p in pts) / len(pts), 1),
            round(sum(p[1] for p in pts) / len(pts), 1)]


def build_source_bim():
    rooms = [
        {"id": "Rbed", "category": "room_bedroom", "area_m2": 9.0,
         "polygon": _rect(0, 0, 3000, 3000),
         "dimensions": {"width_mm": 3000, "length_mm": 3000},
         "name": "Bedroom", "name_source": "ocr", "needs_review": False},
        {"id": "Rkit", "category": "room_kitchen", "area_m2": 6.0,
         "polygon": _rect(3000, 0, 6000, 3000),
         "dimensions": {"width_mm": 3000, "length_mm": 3000},
         "name": "Kitchen", "name_source": "ocr", "needs_review": False},
        {"id": "Rbath", "category": "room_bathroom", "area_m2": 4.0,
         "polygon": _rect(6000, 0, 9000, 3000),
         "dimensions": {"width_mm": 3000, "length_mm": 3000},
         "name": "Bathroom", "name_source": "ocr", "needs_review": False},
    ]
    for r in rooms:
        r["centroid_mm"] = _centroid(r["polygon"])
    return {
        "walls": [
            {"id": "WT", "start_point": [0, 3000, 0], "end_point": [9000, 3000, 0],
             "thickness": 150, "height": 2800, "is_exterior": True},
            {"id": "WL", "start_point": [0, 0, 0], "end_point": [0, 3000, 0],
             "thickness": 150, "height": 2800, "is_exterior": True},
            {"id": "Wbk", "start_point": [3000, 0, 0], "end_point": [3000, 3000, 0],
             "thickness": 100, "height": 2800, "is_exterior": False},
            {"id": "Wkb", "start_point": [6000, 0, 0], "end_point": [6000, 3000, 0],
             "thickness": 100, "height": 2800, "is_exterior": False},
        ],
        "rooms": rooms,
        "doors": [
            {"id": "Dbk", "host_wall_id": "Wbk", "insertion_point": [3000, 1500, 0],
             "width": 900, "height": 2100},
            {"id": "Dkb", "host_wall_id": "Wkb", "insertion_point": [6000, 1500, 0],
             "width": 800, "height": 2100},
            {"id": "Df", "host_wall_id": "WT", "insertion_point": [4500, 3000, 0],
             "width": 1000, "height": 2100},
        ],
        "windows": [{"id": "Wb", "host_wall_id": "WL",
                     "insertion_point": [0, 1500, 0], "width": 1500,
                     "height": 1500, "sill_height": 900}],
        "stairs": [], "slabs": [],
    }


def main():
    sys.path.insert(0, STEP1_REPO)
    from export.ifc_exporter import bim_json_to_ifc

    here = os.path.dirname(os.path.abspath(__file__))
    bim = build_source_bim()
    with open(os.path.join(here, "sample_plan_bim.json"), "w", encoding="utf-8") as f:
        json.dump(bim, f, ensure_ascii=False, indent=2)
    bim_json_to_ifc(bim, {}, os.path.join(here, "sample_plan.ifc"))
    print("Regenerated sample_plan_bim.json + sample_plan.ifc")


if __name__ == "__main__":
    main()
