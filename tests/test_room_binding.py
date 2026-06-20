#!/usr/bin/env python3
"""
test_room_binding.py — OFFLINE Phase 3 verification.

Exercises your REAL analysis/room_analysis.py (room extraction + OCR name
binding + host-wall lookup) on a synthetic two-room floor plan.

Why offline: this needs only numpy + opencv (already in requirements). It needs
NO TensorFlow, NO maskrcnn_15_epochs.h5, and NO graphics server — so you can run
it now, before the full smoke_test.py.

It validates Phase 3's exit criterion: "OCR room names appear and bind to the
correct room polygons."

Place at project root (next to application.py) and run:
    python test_room_binding.py
Exit code 0 = all checks passed.
"""
import sys
import numpy as np

try:
    import cv2
except ImportError:
    print("❌ opencv not importable — `pip install opencv-python-headless`")
    sys.exit(2)

# Import the REAL module under test
try:
    from analysis.room_analysis import extract_room_polygons, find_host_wall_id
except Exception as e:
    print(f"❌ could not import analysis.room_analysis: {type(e).__name__}: {e}")
    print("   Run this from the project root (where the 'analysis' package lives).")
    sys.exit(2)

RESULTS = []
def check(name, fn):
    print(f"\n── {name} " + "─" * max(0, 52 - len(name)))
    try:
        fn(); RESULTS.append((name, True)); print("   ✅ PASS")
    except AssertionError as e:
        RESULTS.append((name, False)); print(f"   ❌ FAIL  {e}")
    except Exception as e:
        RESULTS.append((name, False)); print(f"   ❌ FAIL  {type(e).__name__}: {e}")


def build_two_room_plan(H=600, W=800, t=8):
    """Outer wall box + one interior dividing wall → two roomy chambers."""
    wall = np.zeros((H, W), np.uint8)
    cv2.rectangle(wall, (60, 60), (W - 60, H - 60), 255, t)   # outer walls
    cv2.line(wall, (W // 2, 60), (W // 2, H - 60), 255, t)     # divider
    return wall.astype(bool)


# Scale chosen so each chamber is comfortably above any MIN_ROOM_AREA_M2 cutoff.
# Left/right chambers ≈ 320×480 px → at 15 mm/px ≈ 4800×7200 mm ≈ 34 m² each.
SCALE_MM_PER_PX = 15.0
WALL = build_two_room_plan()
H, W = WALL.shape

# Synthetic OCR results in the format room_analysis expects:
#   insertion_point = [x_px, y_px], plus name / local_name / category
SPACE_NAMES = [
    {"insertion_point": [W * 0.25, H * 0.5], "name": "Bedroom",
     "local_name": "اتاق خواب", "category": "Accommodation"},
    {"insertion_point": [W * 0.75, H * 0.5], "name": "Kitchen",
     "local_name": "آشپزخانه", "category": "Service"},
]

rooms_holder = {}

def _extract_with_ocr():
    rooms = extract_room_polygons(WALL, SCALE_MM_PER_PX, SPACE_NAMES)
    rooms_holder["rooms"] = rooms
    print(f"   rooms returned: {len(rooms)}")
    for r in rooms:
        print(f"     - {r.get('name')!r:12} area_m2={r.get('area_m2')} "
              f"verts={r.get('vertex_count')}")
    assert len(rooms) == 2, f"expected 2 rooms, got {len(rooms)}"
    # each room dict has the documented keys
    for r in rooms:
        for k in ("id", "name", "polygon", "area_m2", "perimeter_m",
                  "centroid_mm", "vertex_count"):
            assert k in r, f"room dict missing key '{k}'"
        assert r["area_m2"] > 0, "area_m2 must be positive"
        assert len(r["polygon"]) >= 4, "polygon must have >= 4 points"
check("extract_room_polygons() — finds 2 rooms with valid geometry", _extract_with_ocr)

def _names_bound():
    rooms = rooms_holder.get("rooms", [])
    names = sorted(r["name"] for r in rooms)
    assert names == ["Bedroom", "Kitchen"], f"OCR names not bound correctly: {names}"
check("OCR names bind to the correct chambers", _names_bound)

def _no_ocr_fallback():
    rooms = extract_room_polygons(WALL, SCALE_MM_PER_PX, None)
    assert len(rooms) == 2, f"no-OCR: expected 2 rooms, got {len(rooms)}"
    # with no OCR, names fall back to a generic label (not crash)
    assert all(isinstance(r.get("name"), str) for r in rooms)
check("no-OCR run still extracts rooms (generic names, no crash)", _no_ocr_fallback)

def _host_wall():
    # two horizontal walls (top and bottom), centerlines in mm
    wall_parameters = [
        {"wall_id": "Wall_Top",    "centerline": [[0, 0],    [5000, 0]]},
        {"wall_id": "Wall_Bottom", "centerline": [[0, 5000], [5000, 5000]]},
    ]
    # a door near the top wall should host to Wall_Top
    host = find_host_wall_id([2500, 120], wall_parameters)
    print(f"   host for point near top wall: {host}")
    assert host == "Wall_Top", f"expected Wall_Top, got {host}"
    # a point with no walls nearby returns None gracefully
    none_host = find_host_wall_id([2500, 2500], [])
    assert none_host is None, f"empty wall list should give None, got {none_host}"
check("find_host_wall_id() — nearest centerline + graceful None", _host_wall)


print("\n" + "=" * 56)
passed = sum(1 for _, ok in RESULTS if ok)
print(f"SUMMARY: {passed}/{len(RESULTS)} checks passed")
print("=" * 56)
sys.exit(0 if passed == len(RESULTS) else 1)
