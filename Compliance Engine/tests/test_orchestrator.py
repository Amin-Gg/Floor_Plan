"""tests/test_orchestrator.py — Step 8 smoke tests."""
import sys
sys.path.insert(0, ".")
from orchestrator import run_compliance, ComplianceResult
from numeric_checker import Verdict

def _rect(x0,y0,x1,y1): return [[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]

# A plan with a known violation: kitchen connects directly to bathroom.
BIM = {
    "walls":[
        {"id":"WT","start_point":[0,3000,0],"end_point":[9000,3000,0],"thickness":150,"is_exterior":True},
        {"id":"WL","start_point":[0,0,0],"end_point":[0,3000,0],"thickness":150,"is_exterior":True},
        {"id":"Wbk","start_point":[3000,0,0],"end_point":[3000,3000,0],"thickness":100,"is_exterior":False},
        {"id":"Wkb","start_point":[6000,0,0],"end_point":[6000,3000,0],"thickness":100,"is_exterior":False},
    ],
    "rooms":[
        {"id":"Rbed","category":"room_bedroom","area_m2":9.0,"polygon":_rect(0,0,3000,3000),"dimensions":{"width_mm":3000,"length_mm":3000}},
        {"id":"Rkit","category":"room_kitchen","area_m2":6.0,"polygon":_rect(3000,0,6000,3000),"dimensions":{"width_mm":3000,"length_mm":2000}},
        {"id":"Rbath","category":"room_bathroom","area_m2":4.0,"polygon":_rect(6000,0,9000,3000),"dimensions":{"width_mm":3000,"length_mm":2000}},
    ],
    "doors":[
        {"id":"Dbk","host_wall_id":"Wbk","insertion_point":[3000,1500,0],"width":900,"height":2100},
        {"id":"Dkb","host_wall_id":"Wkb","insertion_point":[6000,1500,0],"width":800,"height":2100},  # kitchen↔bath
        {"id":"Df","host_wall_id":"WT","insertion_point":[4500,3000,0],"width":1000,"height":2100},
    ],
    "windows":[{"id":"Wb","host_wall_id":"WL","insertion_point":[0,1500,0],"width":1500,"height":1500,"sill_height":900}],
    "stairs":[], "railings":[],
}

CLAUSES = [
    {"article_id":"N1","rule_type":"numeric","text_en":"Bedroom min area 8 m2",
     "entities":{"object":"bedroom","property":"area","comparator":">=","value":8,"unit":"m2","condition":None}},
    {"article_id":"S1","rule_type":"spatial","text_en":"Kitchen must not open into bathroom",
     "entities":{"subject":"kitchen","relation":"must_not_connect_to","object":"bathroom"}},
    {"article_id":"O1","rule_type":"numeric","text_en":"Window area ratio >= 0.125",
     "entities":{"object":"window_area","property":"ratio_to_floor_area","comparator":">=","value":0.125,"unit":"ratio","condition":None}},
    {"article_id":"DEF1","rule_type":"definition","text_en":"A habitable space is...","entities":None},
]

def run():
    # 1. Sequential path (no langgraph dependency)
    res = run_compliance(BIM, CLAUSES, use_langgraph=False)
    assert isinstance(res, ComplianceResult)
    assert res.summary["PASS"] + res.summary["FAIL"] + res.summary["NEEDS_REVIEW"] == len(res.findings)
    print(f"PASS: sequential run → {res.summary}")

    # 2. All four agents contributed
    assert set(res.by_agent.keys()) == {"numeric","topology","opening","safety"}
    print(f"PASS: all 4 agents ran — { {k:len(v) for k,v in res.by_agent.items()} }")

    # 3. The kitchen↔bathroom violation was caught (FAIL somewhere)
    fails = [f for f in res.findings if f.verdict==Verdict.FAIL]
    assert any("kitchen" in f.message.lower() and "bathroom" in f.message.lower() for f in fails)
    print("PASS: kitchen↔bathroom violation caught as FAIL")

    # 4. Bedroom area PASS present
    assert any(f.article_id=="N1" and f.verdict==Verdict.PASS for f in res.findings)
    print("PASS: bedroom area → PASS")

    # 5. LangGraph path produces the SAME summary
    res2 = run_compliance(BIM, CLAUSES, use_langgraph=True)
    assert res2.summary == res.summary, f"{res2.summary} != {res.summary}"
    print(f"PASS: LangGraph path matches sequential → {res2.summary}")

    # 6. Optional LLM pass — supply a fake llm, confirm it annotates NEEDS_REVIEW only
    def fake_llm(prompt): return "Check the site condition manually."
    res3 = run_compliance(BIM, CLAUSES, llm=fake_llm, use_langgraph=False)
    # deterministic verdicts unchanged
    assert res3.summary["PASS"] == res.summary["PASS"]
    assert res3.summary["FAIL"] == res.summary["FAIL"]
    # at least one NEEDS_REVIEW got an AI note
    annotated = [f for f in res3.findings if f.verdict==Verdict.NEEDS_REVIEW and "[AI note:" in f.message]
    assert annotated, "expected at least one AI-annotated review item"
    print(f"PASS: LLM pass annotated {len(annotated)} review items, verdicts unchanged")

    # 7. to_dict() serialises cleanly
    d = res.to_dict()
    assert "summary" in d and "findings" in d and "by_agent" in d
    print("PASS: result serialises to dict")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
