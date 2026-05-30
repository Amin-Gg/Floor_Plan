"""tests/test_report_generator.py — Step 11 smoke tests."""
import sys, os, zipfile
sys.path.insert(0, ".")
from report_generator import generate_reports

# A synthetic ComplianceResult.to_dict() with all three verdict types
RESULT = {
    "summary": {"PASS": 2, "FAIL": 2, "NEEDS_REVIEW": 1},
    "duration_s": 0.84,
    "findings": [
        {"article_id":"4-5-1-3","verdict":"FAIL",
         "message":"Room_kit ↔ Room_bath connected by door D-07",
         "object":"spatial","rule_text_en":"Kitchen must not open into bathroom"},
        {"article_id":"4-6-table-2","verdict":"FAIL",
         "message":"Room_04: 7.0 m² (required >= 8 m²)",
         "object":"bedroom","rule_text_en":"Bedroom minimum floor area"},
        {"article_id":"8-4-4-4","verdict":"NEEDS_REVIEW",
         "message":"Window faces external space — confirm site condition",
         "object":"glass_surface","rule_text_en":"Natural light from open space"},
        {"article_id":"4-3-2-1","verdict":"PASS",
         "message":"Room_01: area = 9.0 m (required >= 8 m)",
         "object":"bedroom","rule_text_en":"Bedroom minimum area"},
        {"article_id":"egress","verdict":"PASS",
         "message":"All habitable rooms reach an exit",
         "object":"egress","rule_text_en":"Egress reachability"},
    ],
}
META = {"plan_name":"Plan_04","occupancy":"M-4 residential","date":"2026-05-29"}

def run():
    out = "/tmp/step11_out"
    paths = generate_reports(RESULT, META, out_dir=out)

    # 1. HTML exists and contains key content
    assert os.path.exists(paths["html"]), "HTML not created"
    html = open(paths["html"], encoding="utf-8").read()
    assert "Mabhas compliance report" in html
    assert "Plan_04" in html
    assert "non-compliant" in html          # has FAILs → non-compliant status
    assert "4-5-1-3" in html                # the failing article id
    assert "Kitchen must not open into bathroom" in html
    print("PASS: HTML created with header, status, and findings")

    # 2. Failures appear before passes (order check)
    i_fail = html.index("4-5-1-3")
    i_pass = html.index("All habitable rooms reach an exit")
    assert i_fail < i_pass, "failures should be ordered before passes"
    print("PASS: findings ordered failures-first")

    # 3. PDF exists (WeasyPrint available here)
    if paths["pdf"]:
        assert os.path.exists(paths["pdf"]), "PDF path returned but file missing"
        size = os.path.getsize(paths["pdf"])
        assert size > 1000, f"PDF suspiciously small: {size} bytes"
        # verify it's a real PDF
        with open(paths["pdf"], "rb") as f:
            assert f.read(5) == b"%PDF-", "not a valid PDF header"
        print(f"PASS: real PDF created ({size:,} bytes, valid header)")
    else:
        print("NOTE: PDF skipped (WeasyPrint unavailable in this env)")

    # 4. BCF exists, is a valid zip, contains version + issue topics
    assert os.path.exists(paths["bcf"]), "BCF not created"
    with zipfile.ZipFile(paths["bcf"]) as z:
        names = z.namelist()
        assert "bcf.version" in names, "bcf.version missing"
        markups = [n for n in names if n.endswith("markup.bcf")]
        # 2 FAIL + 1 NEEDS_REVIEW = 3 issues; 2 PASS excluded
        assert len(markups) == 3, f"expected 3 issue topics, got {len(markups)}"
        # check one markup has the right status
        sample = z.read(markups[0]).decode()
        assert "TopicStatus=" in sample and "<Title>" in sample
    print("PASS: BCF is valid zip with 3 issue topics (passes excluded)")

    # 5. Compliant plan → green status
    clean = {"summary":{"PASS":5,"FAIL":0,"NEEDS_REVIEW":0},"duration_s":0.1,
             "findings":[{"article_id":"x","verdict":"PASS","message":"ok","object":"a","rule_text_en":"r"}]}
    p2 = generate_reports(clean, META, out_dir="/tmp/step11_clean")
    h2 = open(p2["html"], encoding="utf-8").read()
    assert "compliant" in h2 and "non-compliant" not in h2
    print("PASS: all-pass plan → 'compliant' status")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
