"""
Stage 2 — Finding contract + L2 QualityChecker tests.

Invariants locked here:
  * Finding carries category/code/expected/actual and a STABLE finding_id
    (same inputs → same id; different element → different id).
  * to_dict is backward compatible: every pre-Stage-2 key is still present,
    expected/actual fall back to required/measured for numeric findings.
  * run_quality_checks emits first-class QC-* findings from the deficiencies
    the pipeline already detects, in bim_data["_quality"], WITHOUT polluting
    the compliance findings/summary.
  * Unanchored findings are deterministically skipped by strict BCF export.
"""
from __future__ import annotations

import json
import zipfile

from numeric_checker import Finding, Verdict
from tests.helpers import run_quality_checks


def _f(**kw):
    base = dict(article_id="4-5-1", verdict=Verdict.PASS, message="m")
    base.update(kw)
    return Finding(**base)


# ── contract ─────────────────────────────────────────────────────────────────

def test_finding_defaults_are_compliance_category():
    f = _f()
    d = f.to_dict()
    assert d["category"] == "compliance" and d["code"] is None


def test_to_dict_keeps_all_legacy_keys():
    legacy = {"article_id", "verdict", "message", "object", "measured",
              "required", "unit", "element_id", "rule_text_en"}
    assert legacy <= set(_f().to_dict())


def test_expected_actual_fall_back_to_required_measured():
    d = _f(measured=2.6, required=2.4).to_dict()
    assert d["expected"] == 2.4 and d["actual"] == 2.6


def test_explicit_expected_actual_win():
    d = _f(measured=2.6, required=2.4, expected=">= 2.4 m",
           actual="2.6 m").to_dict()
    assert d["expected"] == ">= 2.4 m" and d["actual"] == "2.6 m"


def test_finding_id_stable_and_discriminating():
    a1 = _f(element_id="R1").finding_id
    a2 = _f(element_id="R1").finding_id
    b  = _f(element_id="R2").finding_id
    assert a1 == a2 and a1 != b
    # message wording must NOT change the id (reports may be reworded)
    assert _f(element_id="R1", message="other wording").finding_id == a1


# ── quality checker ──────────────────────────────────────────────────────────

def _bim(**kw):
    base = {
        "rooms": [], "doors": [], "windows": [],
        "_review_summary": {"threshold": 0.5, "flagged": [],
                            "scale_flagged": False, "scale_confidence": None},
    }
    base.update(kw)
    return base


def test_clean_model_passes_quality():
    stage = run_quality_checks(_bim())
    assert stage["status"] == "passed" and stage["findings"] == []


def test_unmapped_room_yields_space_tag_001():
    bim = _bim(rooms=[{"id": "R1", "category": "room_bedroom",
                       "category_raw": "اتاق ناشناخته",
                       "category_source": "unmapped",
                       "category_confidence": 0.0}])
    stage = run_quality_checks(bim)
    codes = [f["code"] for f in stage["findings"]]
    assert "QC-SPACE-TAG-001" in codes
    f = next(row for row in stage["findings"]
             if row["code"] == "QC-SPACE-TAG-001")
    assert f["category"] == "quality" and f["verdict"] == "NOT_EVALUATED"
    assert f["element_id"] == "R1" and f["finding_id"]


def test_low_confidence_mapping_yields_space_tag_002():
    bim = _bim(rooms=[{"id": "R1", "category": "room_bedroom",
                       "category_raw": "bed rm",
                       "category_source": "label",
                       "category_confidence": 0.3}])
    stage = run_quality_checks(bim)
    match = next(row for row in stage["findings"]
                 if row["code"] == "QC-SPACE-TAG-002")
    assert match["actual"] == "0.30"


def test_flagged_element_yields_elem_conf_001():
    bim = _bim()
    bim["_review_summary"]["flagged"] = [
        {"collection": "walls", "id": "W7",
         "reason": "detector confidence 0.30 < threshold 0.50",
         "confidence": 0.30}]
    stage = run_quality_checks(bim)
    assert [f["code"] for f in stage["findings"]] == ["QC-ELEM-CONF-001"]
    assert stage["findings"][0]["element_id"] == "W7"


def test_low_scale_reported_once_globally_not_per_element():
    bim = _bim()
    bim["_review_summary"].update(scale_flagged=True, scale_confidence=0.2)
    bim["_review_summary"]["flagged"] = [
        {"collection": "rooms", "id": f"R{i}",
         "reason": "scale confidence 0.20 < 0.50 (source: default) — "
                   "dimensional checks unreliable until the plan is re-scaled",
         "confidence": 1.0} for i in range(4)]
    stage = run_quality_checks(bim)
    codes = [f["code"] for f in stage["findings"]]
    assert codes == ["QC-SCALE-001"]


def test_unasserted_verdict_param_yields_param_001():
    bim = _bim(building_params={"wall_height": 2800.0, "_provided": []})
    stage = run_quality_checks(bim)
    assert [f["code"] for f in stage["findings"]] == ["QC-PARAM-001"]


def test_asserted_param_is_clean():
    bim = _bim(building_params={"wall_height": 3000.0,
                                "_provided": ["wall_height"]})
    assert run_quality_checks(bim)["status"] == "passed"


def test_quality_stage_lands_in_pipeline_payload_without_summary_pollution():
    import json, os
    os.environ["LLM_PASS_ENABLED"] = "0"
    from tests.helpers import run_ifc_compliance
    fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                           "sample_plan.ifc")
    result, bim = run_ifc_compliance(fixture, clauses=[])
    q = bim.get("_quality")
    assert q and q["stage"] == "quality"
    # L2 findings must NOT leak into the L3 findings/summary: every finding in
    # the compliance result is category="compliance" (built-in agent checks,
    # e.g. natural-light presence, still run on an empty clause corpus), and
    # the summary counts exactly those.
    assert all(f.category == "compliance" for f in result.findings)
    assert sum(result.summary.values()) == len(result.findings)


# ── deterministic BCF ────────────────────────────────────────────────────────

def test_finding_ids_unique_within_a_full_run():
    """Every finding_id in a real full-corpus run must be unique — duplicate
    BCF topic GUIDs make readers silently drop topics (defect found when
    deterministic GUIDs replaced uuid4)."""
    import json, os
    os.environ["LLM_PASS_ENABLED"] = "0"
    from tests.helpers import run_ifc_compliance
    fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                           "sample_plan.ifc")
    clauses = [c for c in json.load(open("data/mabhas_clauses.json"))
               if not c.get("skip_category")]
    result, _ = run_ifc_compliance(fixture, clauses)
    ids = [f.finding_id for f in result.findings]
    assert len(ids) == len(set(ids)), "finding_id collision in one run"


def test_unanchored_bcf_skip_manifest_is_deterministic_across_runs(tmp_path):
    from reporting.generator import generate_report_bundle
    result = {
        "summary": {"PASS": 0, "FAIL": 1, "NEEDS_REVIEW": 0,
                    "NOT_EVALUATED": 0},
        "duration_s": 0.0, "by_agent": {},
        "findings": [Finding(article_id="4-5-1", verdict=Verdict.FAIL,
                             message="too small", element_id="R1",
                             object="bedroom").to_dict()],
    }
    manifests = []
    for directory in ("a", "b"):
        paths = generate_report_bundle(
            result, {"plan_name": "t"}, output_dir=str(tmp_path / directory)
        )
        with zipfile.ZipFile(paths["bcf"]) as archive:
            assert not any(name.endswith("markup.bcf") for name in archive.namelist())
        manifests.append(json.load(open(paths["bcf"] + ".manifest.json", encoding="utf-8")))
    assert manifests[0]["topics_total"] == manifests[1]["topics_total"] == 0
    assert manifests[0]["skipped"] == manifests[1]["skipped"]
    assert "no trustworthy IFC GlobalId" in manifests[0]["skipped"][0]["reason"]
