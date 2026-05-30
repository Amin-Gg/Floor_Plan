"""
tests/test_spatial_graph.py
============================
Smoke test for spatial_graph.py using a synthetic bim_data that mirrors
the exact structure BimDataBuilder produces.

Run:   pip install networkx shapely
       python tests/test_spatial_graph.py
"""

import sys, math
sys.path.insert(0, ".")
from spatial_graph import SpatialGraph, _xy

# ── Synthetic bim_data (3 rooms, 2 doors, 3 windows) ─────────────────────────
#
#   [  BEDROOM  ]--Door_1--[ LIVING ]--Door_2--[ BATHROOM ]
#        |                    |
#    Window_1             Window_2 (exterior)
#                          Door_2 also has Window_3
#
#   The LIVING room has an exterior door (front door) → exit point.
#   Bedroom has one exterior window.
#
#  Coordinates (mm, top-left origin):
#   Bedroom  : (0,0)   → (3000, 3000)
#   Living   : (3000,0) → (7000, 3000)
#   Bathroom : (7000,0) → (9000, 3000)

def _rect(x0, y0, x1, y1):
    return [[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]

BIM_DATA = {
    "walls": [
        {"id":"W1","start_point":[0,0,0],   "end_point":[9000,0,0],   "thickness":150,"is_exterior":True},
        {"id":"W2","start_point":[0,3000,0],"end_point":[9000,3000,0],"thickness":150,"is_exterior":True},
        {"id":"W3","start_point":[0,0,0],   "end_point":[0,3000,0],   "thickness":150,"is_exterior":True},
        {"id":"W4","start_point":[9000,0,0],"end_point":[9000,3000,0],"thickness":150,"is_exterior":True},
        {"id":"W5","start_point":[3000,0,0],"end_point":[3000,3000,0],"thickness":150,"is_exterior":False},
        {"id":"W6","start_point":[7000,0,0],"end_point":[7000,3000,0],"thickness":150,"is_exterior":False},
    ],
    "rooms": [
        {"id":"Room_1","name":"Bedroom","category":"room_bedroom",
         "polygon":_rect(0,0,3000,3000),"area_m2":9.0},
        {"id":"Room_2","name":"Living Room","category":"room_living",
         "polygon":_rect(3000,0,7000,3000),"area_m2":12.0},
        {"id":"Room_3","name":"Bathroom","category":"room_bathroom",
         "polygon":_rect(7000,0,9000,3000),"area_m2":4.0},
    ],
    "doors": [
        # Door between Bedroom and Living (on wall W5)
        {"id":"Door_1","host_wall_id":"W5",
         "insertion_point":[3000,1500,0],"width":900,"height":2100},
        # Door between Living and Bathroom (on wall W6)
        {"id":"Door_2","host_wall_id":"W6",
         "insertion_point":[7000,1500,0],"width":800,"height":2100},
        # Front door from Living to exterior (on exterior wall W2)
        {"id":"Door_3","host_wall_id":"W2",
         "insertion_point":[5000,3000,0],"width":1000,"height":2100},
    ],
    "windows": [
        # Bedroom exterior window (on W3)
        {"id":"Window_1","host_wall_id":"W3",
         "insertion_point":[0,1500,0],"width":1200,"height":1000,"sill_height":900},
        # Living room exterior window (on W1)
        {"id":"Window_2","host_wall_id":"W1",
         "insertion_point":[5000,0,0],"width":1500,"height":1000,"sill_height":900},
        # Bathroom no exterior window (internal wall W6 — should NOT be exterior)
        {"id":"Window_3","host_wall_id":"W6",
         "insertion_point":[7000,500,0],"width":600,"height":400,"sill_height":1500},
    ],
    "stairs": [],
    "slabs":  [],
}


def run():
    sg = SpatialGraph(BIM_DATA)

    # 1. Correct number of nodes and edges
    assert sg.graph.number_of_nodes() == 3, f"Expected 3 nodes, got {sg.graph.number_of_nodes()}"
    print("PASS: 3 room nodes")

    # 2. Bedroom–Living door edge exists
    assert sg.are_directly_connected("Room_1","Room_2"), "Bedroom-Living edge missing"
    print("PASS: Bedroom ↔ Living connected by door")

    # 3. Living–Bathroom door edge exists
    assert sg.are_directly_connected("Room_2","Room_3"), "Living-Bathroom edge missing"
    print("PASS: Living ↔ Bathroom connected by door")

    # 4. Bedroom NOT directly connected to Bathroom
    assert not sg.are_directly_connected("Room_1","Room_3"), "Bedroom should NOT connect to Bathroom"
    print("PASS: Bedroom ↛ Bathroom (no direct door)")

    # 5. Living room has exterior door (front door Door_3)
    assert sg.graph.nodes["Room_2"].get("has_exterior_door"), "Living room should have exterior door"
    print("PASS: Living room has exterior door")

    # 6. Egress: all rooms can reach exit via Living
    for rid in ["Room_1","Room_2","Room_3"]:
        assert sg.can_reach_exit(rid), f"{rid} cannot reach exit"
    print("PASS: all 3 rooms can reach exit")

    # 7. Egress path from Bedroom goes through Living
    path = sg.egress_path("Room_1")
    assert path is not None and "Room_2" in path, f"Expected Room_2 in path, got {path}"
    print(f"PASS: egress path Bedroom→exit = {path}")

    # 8. Window assignment — Bedroom gets Window_1
    bedroom_wins = sg.graph.nodes["Room_1"]["windows"]
    assert any(w["id"]=="Window_1" for w in bedroom_wins), "Window_1 not assigned to Bedroom"
    print("PASS: Window_1 assigned to Bedroom")

    # 9. Exterior windows — Window_1 (W3 exterior) should be exterior
    ext_wins = sg.get_exterior_windows("Room_1")
    assert any(w["id"]=="Window_1" for w in ext_wins), "Window_1 should be exterior"
    print("PASS: Window_1 detected as exterior")

    # 10. Glazing ratio for Bedroom: 1.2m × 1.0m = 1.2m² / 9m² ≈ 0.133
    ratio = sg.glazing_ratio("Room_1")
    assert abs(ratio - 1.2/9.0) < 0.01, f"Glazing ratio wrong: {ratio:.4f}"
    print(f"PASS: Bedroom glazing ratio = {ratio:.3f} (1.2m²/9m²)")

    # 11. get_rooms_by_category
    bedrooms = sg.get_rooms_by_category("room_bedroom")
    assert bedrooms == ["Room_1"], f"Expected ['Room_1'], got {bedrooms}"
    print("PASS: get_rooms_by_category('room_bedroom') = ['Room_1']")

    # 12. summary dict has correct counts
    s = sg.summary()
    assert s["total_rooms"] == 3
    assert s["total_doors"] == 2    # interior doors only (Door_3 → exterior_door flag)
    assert s["exit_rooms"]  == 1    # only Living has exterior door
    print(f"PASS: summary = {s}")

    # 13. _xy helper handles both list and dict forms
    assert _xy([100,200,0]) == (100.0,200.0)
    assert _xy({"x":100,"y":200}) == (100.0,200.0)
    print("PASS: _xy handles list and dict")

    print("\n=== ALL 13 TESTS PASSED ===")


if __name__ == "__main__":
    run()
