"""
tests/test_numeric_checker.py
=============================
Smoke tests for Step 9. Uses synthetic bim_data + synthetic numeric clauses
that mirror the real corpus shapes (dict-form, list-form, ratios, conditions).
"""
import sys
sys.path.insert(0, ".")
from numeric_checker import NumericChecker, Verdict, summarise

# ── Synthetic bim_data ────────────────────────────────────────────────────────
BIM = {
    "rooms": [
        {"id":"Room_1","category":"room_bedroom","area_m2":9.0,
         "dimensions":{"width_mm":2800,"length_mm":3200}},
        {"id":"Room_2","category":"room_kitchen","area_m2":5.5,
         "dimensions":{"width_mm":2000,"length_mm":2750}},
        {"id":"Room_3","category":"room_bathroom","area_m2":2.8,
         "dimensions":{"width_mm":1400,"length_mm":2000}},
        {"id":"Room_4","category":"room_bedroom","area_m2":7.0,   # SMALL bedroom
         "dimensions":{"width_mm":2500,"length_mm":2800}},
    ],
    "doors": [
        {"id":"Door_1","width":900},   # 0.9 m — ok
        {"id":"Door_2","width":700},   # 0.7 m — narrow
    ],
    "windows": [],
}

# ── Synthetic clauses covering every code path ────────────────────────────────
CLAUSES = [
    # 1. bedroom area >= 8 m²  → Room_1 PASS (9), Room_4 FAIL (7)
    {"article_id":"T-AREA","rule_type":"numeric","text_en":"Bedroom min area 8 m2",
     "entities":{"object":"bedroom","property":"area","comparator":">=","value":8,"unit":"m2","condition":None}},

    # 2. kitchen area >= 6 m²  → Room_2 FAIL (5.5)
    {"article_id":"T-KIT","rule_type":"numeric","text_en":"Kitchen min area 6 m2",
     "entities":{"object":"kitchen","property":"area","comparator":">=","value":6,"unit":"m2","condition":None}},

    # 3. door width >= 800 mm  → Door_1 PASS (900), Door_2 FAIL (700)
    {"article_id":"T-DOOR","rule_type":"numeric","text_en":"Door min width 800 mm",
     "entities":{"object":"door_width","property":"width","comparator":">=","value":800,"unit":"mm","condition":None}},

    # 4. RATIO unit → NEEDS_REVIEW (not auto-checkable)
    {"article_id":"T-RATIO","rule_type":"numeric","text_en":"Intermediate floor area ratio",
     "entities":{"object":"intermediate floor","property":"area","comparator":"<=","value":0.33,"unit":"ratio","condition":None}},

    # 5. CONDITION present → NEEDS_REVIEW
    {"article_id":"T-COND","rule_type":"numeric","text_en":"Glass surface adjacent open space",
     "entities":{"object":"glass_surface","property":"width","comparator":">=","value":90,"unit":"m","condition":"adjacent_to open_space"}},

    # 6. UNMAPPED object → NEEDS_REVIEW
    {"article_id":"T-UNMAP","rule_type":"numeric","text_en":"Courtyard dimension",
     "entities":{"object":"courtyard","property":"width","comparator":">=","value":3,"unit":"m","condition":None}},

    # 7. LIST-form entities (multi-threshold) — one mapped (door_width), one not
    {"article_id":"T-LIST","rule_type":"numeric","text_en":"Stair tread and riser",
     "entities":[
        {"object":"door_width","property":"width","comparator":">=","value":750,"unit":"mm","condition":None},
        {"object":"stair_riser","property":"height","comparator":"range","value":[0.175,0.185],"unit":"m","condition":None},
     ]},
]

def run():
    chk = NumericChecker(BIM)
    findings = chk.check_all(CLAUSES)

    by_art = {}
    for f in findings:
        by_art.setdefault(f.article_id, []).append(f)

    # T-AREA: two bedrooms → one PASS (Room_1=9), one FAIL (Room_4=7)
    area = by_art["T-AREA"]
    verdicts = sorted(f.verdict for f in area)
    assert Verdict.PASS in verdicts and Verdict.FAIL in verdicts, f"T-AREA: {verdicts}"
    fail = next(f for f in area if f.verdict==Verdict.FAIL)
    assert fail.element_id == "Room_4" and fail.measured == 7.0
    print("PASS: bedroom area — Room_1 PASS (9m²), Room_4 FAIL (7m²)")

    # T-KIT: kitchen 5.5 < 6 → FAIL
    kit = by_art["T-KIT"]
    assert all(f.verdict==Verdict.FAIL for f in kit) and kit[0].measured==5.5
    print("PASS: kitchen area 5.5m² < 6m² → FAIL")

    # T-DOOR: 0.9 PASS, 0.7 FAIL
    door = by_art["T-DOOR"]
    dv = {f.element_id: f.verdict for f in door}
    assert dv["Door_1"]==Verdict.PASS and dv["Door_2"]==Verdict.FAIL
    print("PASS: door width — Door_1 (900mm) PASS, Door_2 (700mm) FAIL")

    # T-RATIO: ratio unit → NEEDS_REVIEW
    assert all(f.verdict==Verdict.NEEDS_REVIEW for f in by_art["T-RATIO"])
    print("PASS: ratio unit → NEEDS_REVIEW (not guessed)")

    # T-COND: condition → NEEDS_REVIEW
    assert all(f.verdict==Verdict.NEEDS_REVIEW for f in by_art["T-COND"])
    print("PASS: conditional rule → NEEDS_REVIEW")

    # T-UNMAP: unmapped object → NEEDS_REVIEW
    assert all(f.verdict==Verdict.NEEDS_REVIEW for f in by_art["T-UNMAP"])
    print("PASS: unmapped object 'courtyard' → NEEDS_REVIEW")

    # T-LIST: door_width entity checked (2 doors), stair_riser → NEEDS_REVIEW
    lst = by_art["T-LIST"]
    door_findings = [f for f in lst if f.object=="door_width"]
    riser_findings = [f for f in lst if f.object=="stair_riser"]
    assert len(door_findings)==2, f"expected 2 door findings, got {len(door_findings)}"
    assert all(f.verdict==Verdict.NEEDS_REVIEW for f in riser_findings)
    print("PASS: list-form entities — door checked, unmapped riser → NEEDS_REVIEW")

    # Units: 800mm threshold vs 900mm door correctly compared in metres
    # (already covered by T-DOOR — confirms mm→m normalisation works)
    print("PASS: unit normalisation (mm threshold vs mm bim value) correct")

    s = summarise(findings)
    print(f"\nSummary: {s}")
    assert s["PASS"] >= 2 and s["FAIL"] >= 3 and s["NEEDS_REVIEW"] >= 4
    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
