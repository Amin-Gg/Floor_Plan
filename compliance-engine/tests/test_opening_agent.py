"""
tests/test_opening_agent.py — Step 6 smoke tests."""
import sys
sys.path.insert(0, ".")
from spatial_graph import SpatialGraph
from opening_agent import OpeningAgent
from numeric_checker import Verdict

def _rect(x0,y0,x1,y1): return [[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]

# Bedroom with a big window (good ratio), Kitchen with a tiny window (bad ratio),
# Bathroom with NO exterior window.
BIM = {
    "walls":[
        {"id":"W_left","start_point":[0,0,0],"end_point":[0,3000,0],"thickness":150,"is_exterior":True},
        {"id":"W_top","start_point":[0,3000,0],"end_point":[9000,3000,0],"thickness":150,"is_exterior":True},
        {"id":"W_bot","start_point":[0,0,0],"end_point":[9000,0,0],"thickness":150,"is_exterior":True},
        {"id":"W_int1","start_point":[3000,0,0],"end_point":[3000,3000,0],"thickness":100,"is_exterior":False},
        {"id":"W_int2","start_point":[6000,0,0],"end_point":[6000,3000,0],"thickness":100,"is_exterior":False},
    ],
    "rooms":[
        {"id":"R_bed","category":"room_bedroom","area_m2":9.0,"polygon":_rect(0,0,3000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"R_kit","category":"room_kitchen","area_m2":9.0,"polygon":_rect(3000,0,6000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"R_bath","category":"room_bathroom","area_m2":4.0,"polygon":_rect(6000,0,9000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
    ],
    "doors":[],
    "windows":[
        # Bedroom: 1.5m x 1.5m = 2.25 m² / 9 m² = 0.25 ratio (good, on exterior left wall)
        {"id":"Win_bed","host_wall_id":"W_left","insertion_point":[0,1500,0],"width":1500,"height":1500,"sill_height":900},
        # Kitchen: 0.6m x 0.5m = 0.3 m² / 9 m² = 0.033 ratio (bad, on exterior bottom wall)
        {"id":"Win_kit","host_wall_id":"W_bot","insertion_point":[4500,0,0],"width":600,"height":500,"sill_height":900},
        # Bathroom: NO window
    ],
}

CLAUSES = [
    # 1. glazing ratio >= 0.125 → Kitchen (0.033) FAILS, Bedroom (0.25) passes → overall FAIL
    {"article_id":"O-RATIO","rule_type":"numeric","text_en":"Window area >= 1/8 floor area",
     "entities":{"object":"window_area","property":"ratio_to_floor_area","comparator":">=","value":0.125,"unit":"ratio","condition":None}},

    # 2. site-dependent (open space) → NEEDS_REVIEW
    {"article_id":"O-SITE","rule_type":"numeric","text_en":"Glass surface adjacent open space",
     "entities":{"object":"glass_surface","property":"width","comparator":">=","value":90,"unit":"m","condition":"adjacent_to open_space_or_passage"}},

    # 3. light well width → NEEDS_REVIEW (site dependent keyword 'light well')
    {"article_id":"O-WELL","rule_type":"numeric","text_en":"Light well minimum width",
     "entities":{"object":"light well","property":"width","comparator":">=","value":1.25,"unit":"m","condition":None}},

    # 4. non-window numeric rule → returns NOTHING (not this agent's domain)
    {"article_id":"O-NOTMINE","rule_type":"numeric","text_en":"Door width",
     "entities":{"object":"door_width","property":"width","comparator":">=","value":800,"unit":"mm","condition":None}},
]

def run():
    sg = SpatialGraph(BIM)
    agent = OpeningAgent(sg)
    findings = agent.check_all(CLAUSES)
    by_art = {f.article_id: f for f in findings}

    # 1. glazing ratio → FAIL (kitchen drags it down)
    assert "O-RATIO" in by_art, "glazing ratio rule not checked"
    assert by_art["O-RATIO"].verdict == Verdict.FAIL, by_art["O-RATIO"].verdict
    assert by_art["O-RATIO"].element_id == "R_kit"
    print(f"PASS: glazing ratio → FAIL on kitchen — {by_art['O-RATIO'].message}")

    # 2. site-dependent open space → NEEDS_REVIEW
    assert by_art["O-SITE"].verdict == Verdict.NEEDS_REVIEW
    print("PASS: 'adjacent open space' → NEEDS_REVIEW (site condition)")

    # 3. light well → NEEDS_REVIEW
    assert "O-WELL" in by_art and by_art["O-WELL"].verdict == Verdict.NEEDS_REVIEW
    print("PASS: 'light well' → NEEDS_REVIEW (site condition)")

    # 4. door rule → not claimed by this agent
    assert "O-NOTMINE" not in by_art
    print("PASS: door rule correctly ignored (not opening agent's domain)")

    # 5. natural light presence: kitchen HAS a window (passes), but let's verify
    #    the check works by confirming bedroom+kitchen are NOT flagged (both have windows)
    #    and that a light-required room without a window WOULD be caught.
    light = agent.check_light_presence()
    # Bathroom is not in LIGHT_REQUIRED_CATEGORIES (can be mechanically ventilated)
    # so it should NOT appear. Bedroom and kitchen both have windows → not flagged.
    flagged_ids = {f.element_id for f in light}
    assert "R_bed" not in flagged_ids, "bedroom has window, should not be flagged"
    assert "R_kit" not in flagged_ids, "kitchen has window, should not be flagged"
    print("PASS: rooms with windows not flagged for missing light")

    # 6. Add a windowless living room and confirm it IS caught
    bim2 = {**BIM, "rooms": BIM["rooms"] + [
        {"id":"R_dark","category":"room_living","area_m2":12.0,
         "polygon":_rect(0,3500,4000,6500),"dimensions":{"width_mm":4000,"length_mm":3000}}]}
    sg2 = SpatialGraph(bim2)
    agent2 = OpeningAgent(sg2)
    light2 = agent2.check_light_presence()
    dark_fail = [f for f in light2 if f.element_id=="R_dark"]
    assert dark_fail and dark_fail[0].verdict==Verdict.FAIL
    print("PASS: windowless living room → natural light FAIL")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
