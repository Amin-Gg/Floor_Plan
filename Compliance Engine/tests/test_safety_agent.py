"""tests/test_safety_agent.py — Step 7 smoke tests."""
import sys
sys.path.insert(0, ".")
from spatial_graph import SpatialGraph
from safety_agent import SafetyAgent
from numeric_checker import Verdict

def _rect(x0,y0,x1,y1): return [[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]

# Plan A: bedroom + living with front door (egress OK), has a stair + railing, has balcony
BIM_OK = {
    "walls":[
        {"id":"WT","start_point":[0,3000,0],"end_point":[6000,3000,0],"thickness":150,"is_exterior":True},
        {"id":"Wint","start_point":[3000,0,0],"end_point":[3000,3000,0],"thickness":100,"is_exterior":False},
    ],
    "rooms":[
        {"id":"Rbed","category":"room_bedroom","area_m2":9.0,"polygon":_rect(0,0,3000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"Rliv","category":"room_living","area_m2":12.0,"polygon":_rect(3000,0,6000,3000),"dimensions":{"width_mm":3000,"length_mm":4000}},
        {"id":"Rbal","category":"balcony","area_m2":3.0,"polygon":_rect(6000,0,7500,2000),"dimensions":{"width_mm":1500,"length_mm":2000}},
    ],
    "doors":[
        {"id":"Dbl","host_wall_id":"Wint","insertion_point":[3000,1500,0],"width":900,"height":2100},
        {"id":"Dfront","host_wall_id":"WT","insertion_point":[4500,3000,0],"width":1000,"height":2100},
    ],
    "windows":[],
    "stairs":[{"id":"Stair_1","footprint_polygon":_rect(3000,0,4000,1500)}],
    "railings":[{"id":"Rail_1"}],
}

# Plan B: bedroom that CANNOT reach exit (no front door), no stair, balcony but NO railing
BIM_BAD = {
    "walls":[{"id":"Wint","start_point":[3000,0,0],"end_point":[3000,3000,0],"thickness":100,"is_exterior":False}],
    "rooms":[
        {"id":"Rbed","category":"room_bedroom","area_m2":9.0,"polygon":_rect(0,0,3000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"Rbal","category":"balcony","area_m2":3.0,"polygon":_rect(3000,0,4500,2000),"dimensions":{"width_mm":1500,"length_mm":2000}},
    ],
    "doors":[],   # no exterior door at all
    "windows":[],
    "stairs":[],
    "railings":[],
}

CLAUSES = [
    {"article_id":"SF-EGRESS","rule_type":"spatial","text_en":"Egress routes must be provided",
     "entities":{"subject":"egress_routes","relation":"must_provide","object":"safe escape"}},
    {"article_id":"SF-STAIR","rule_type":"spatial","text_en":"Roof access via stairway",
     "entities":{"subject":"roof_access","relation":"must_be_provided_through","object":"stairway"}},
    {"article_id":"SF-GUARD","rule_type":"spatial","text_en":"Balcony must have guard",
     "entities":{"subject":"balcony_or_terrace_floor","relation":"must_have_refuge_or_guard","object":"guard"}},
    {"article_id":"SF-NOTMINE","rule_type":"spatial","text_en":"Kitchen must not connect to bathroom",
     "entities":{"subject":"kitchen","relation":"must_not_connect_to","object":"bathroom"}},
]

def run():
    # ---- Plan A (compliant) ----
    sgA = SpatialGraph(BIM_OK)
    agentA = SafetyAgent(sgA, BIM_OK)
    fA = {f.article_id: f for f in agentA.check_all(CLAUSES)}

    # stair present → PASS
    assert fA["SF-STAIR"].verdict == Verdict.PASS, fA["SF-STAIR"].verdict
    print(f"PASS: stair present → PASS — {fA['SF-STAIR'].message}")

    # balcony + railing → guard PASS
    assert fA["SF-GUARD"].verdict == Verdict.PASS
    print("PASS: balcony with railing → guard PASS")

    # egress reachable → NEEDS_REVIEW (reachability ok, details need review)
    assert fA["SF-EGRESS"].verdict == Verdict.NEEDS_REVIEW
    print("PASS: egress reachable → NEEDS_REVIEW (details)")

    # kitchen rule not claimed by safety agent
    assert "SF-NOTMINE" not in fA
    print("PASS: non-safety rule correctly ignored")

    # standalone egress check — all rooms reach exit
    egA = agentA.check_egress_all_rooms()
    assert len(egA) == 0, f"expected no egress failures, got {egA}"
    print("PASS: all habitable rooms reach exit (Plan A)")

    # ---- Plan B (non-compliant) ----
    sgB = SpatialGraph(BIM_BAD)
    agentB = SafetyAgent(sgB, BIM_BAD)
    fB = {f.article_id: f for f in agentB.check_all(CLAUSES)}

    # egress: bedroom can't reach exit → FAIL
    assert fB["SF-EGRESS"].verdict == Verdict.FAIL, fB["SF-EGRESS"].verdict
    print(f"PASS: no exit → egress FAIL — {fB['SF-EGRESS'].message}")

    # balcony but no railing → guard FAIL
    assert fB["SF-GUARD"].verdict == Verdict.FAIL
    print("PASS: balcony without railing → guard FAIL")

    # stair clause but no stair → NEEDS_REVIEW (might be single storey)
    assert fB["SF-STAIR"].verdict == Verdict.NEEDS_REVIEW
    print("PASS: stair rule, no stair → NEEDS_REVIEW (not auto-fail)")

    # standalone egress — no exterior door at all → one FAIL finding
    egB = agentB.check_egress_all_rooms()
    assert egB and egB[0].verdict == Verdict.FAIL
    print(f"PASS: plan with no exit → egress presence FAIL — {egB[0].message}")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
