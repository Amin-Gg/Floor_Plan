"""
tests/test_topology_agent.py
============================
Smoke tests for Step 5. Builds a real SpatialGraph from synthetic bim_data,
then runs the topology agent against synthetic spatial clauses covering every
code path.

Layout:
   [BEDROOM]--[LIVING]--[KITCHEN]--[BATHROOM]
   Kitchen connects DIRECTLY to Bathroom (a violation we want to catch).
"""
import sys
sys.path.insert(0, ".")
from spatial_graph import SpatialGraph
from topology_agent import TopologyAgent
from numeric_checker import Verdict

def _rect(x0,y0,x1,y1):
    return [[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]

# Bedroom(0-3) Living(3-6) Kitchen(6-9) Bathroom(9-11), all 3m tall
BIM = {
    "walls":[
        {"id":"W_ext_bottom","start_point":[0,0,0],"end_point":[11000,0,0],"thickness":150,"is_exterior":True},
        {"id":"W_ext_top","start_point":[0,3000,0],"end_point":[11000,3000,0],"thickness":150,"is_exterior":True},
        {"id":"W_bed_liv","start_point":[3000,0,0],"end_point":[3000,3000,0],"thickness":100,"is_exterior":False},
        {"id":"W_liv_kit","start_point":[6000,0,0],"end_point":[6000,3000,0],"thickness":100,"is_exterior":False},
        {"id":"W_kit_bath","start_point":[9000,0,0],"end_point":[9000,3000,0],"thickness":100,"is_exterior":False},
    ],
    "rooms":[
        {"id":"Room_bed","category":"room_bedroom","area_m2":9.0,"polygon":_rect(0,0,3000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"Room_liv","category":"room_living","area_m2":9.0,"polygon":_rect(3000,0,6000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"Room_kit","category":"room_kitchen","area_m2":9.0,"polygon":_rect(6000,0,9000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"Room_bath","category":"room_bathroom","area_m2":6.0,"polygon":_rect(9000,0,11000,3000),"dimensions":{"width_mm":2000,"length_mm":3000}},
    ],
    "doors":[
        {"id":"D_bed_liv","host_wall_id":"W_bed_liv","insertion_point":[3000,1500,0],"width":900,"height":2100},
        {"id":"D_liv_kit","host_wall_id":"W_liv_kit","insertion_point":[6000,1500,0],"width":900,"height":2100},
        {"id":"D_kit_bath","host_wall_id":"W_kit_bath","insertion_point":[9000,1500,0],"width":800,"height":2100},  # VIOLATION
        {"id":"D_front","host_wall_id":"W_ext_top","insertion_point":[4500,3000,0],"width":1000,"height":2100},  # exit
    ],
    "windows":[],
}

CLAUSES = [
    # 1. kitchen must_not_connect_to bathroom → FAIL (they connect via D_kit_bath)
    {"article_id":"S-KITBATH","rule_type":"spatial","text_en":"Kitchen must not open into bathroom",
     "entities":{"subject":"kitchen","relation":"must_not_connect_to","object":"toilet or bathroom"}},

    # 2. bedroom must_not_connect_to kitchen → PASS (not directly connected; via living)
    {"article_id":"S-BEDKIT","rule_type":"spatial","text_en":"Bedroom must not open into kitchen",
     "entities":{"subject":"bedroom","relation":"must_not_connect_to","object":"kitchen"}},

    # 3. bedroom must_have_access_to exit → PASS (reachable via living→front door)
    {"article_id":"S-ACCESS","rule_type":"spatial","text_en":"Bedroom must have access to exit",
     "entities":{"subject":"bedroom","relation":"must_have_access_to","object":"exit"}},

    # 4. unmapped relation → NEEDS_REVIEW
    {"article_id":"S-UNMAP","rule_type":"spatial","text_en":"Facade must not face street",
     "entities":{"subject":"continuous_glass_facade","relation":"must_not_face","object":"street"}},

    # 5. unmappable subject category → NEEDS_REVIEW
    {"article_id":"S-NOCAT","rule_type":"spatial","text_en":"Protrusion must not be in corridor",
     "entities":{"subject":"protrusion","relation":"must_not_connect_to","object":"electric corridor"}},
]

def run():
    sg = SpatialGraph(BIM)
    agent = TopologyAgent(sg)
    findings = agent.check_all(CLAUSES)
    by_art = {f.article_id: f for f in findings}

    # 1. kitchen↔bathroom → FAIL
    assert by_art["S-KITBATH"].verdict == Verdict.FAIL, by_art["S-KITBATH"].verdict
    print(f"PASS: kitchen↔bathroom detected as FAIL — {by_art['S-KITBATH'].message}")

    # 2. bedroom not connected to kitchen → PASS
    assert by_art["S-BEDKIT"].verdict == Verdict.PASS
    print("PASS: bedroom↛kitchen (not directly connected) → PASS")

    # 3. bedroom can reach exit → PASS
    assert by_art["S-ACCESS"].verdict == Verdict.PASS
    print("PASS: bedroom has access to exit → PASS")

    # 4. unmapped relation → NEEDS_REVIEW
    assert by_art["S-UNMAP"].verdict == Verdict.NEEDS_REVIEW
    print("PASS: unmapped relation 'must_not_face' → NEEDS_REVIEW")

    # 5. unmappable subject → NEEDS_REVIEW
    assert by_art["S-NOCAT"].verdict == Verdict.NEEDS_REVIEW
    print("PASS: unmappable subject 'protrusion' → NEEDS_REVIEW")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
