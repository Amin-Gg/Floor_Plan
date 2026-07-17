"""
Stage 3 — QC-PLACE placement checks + staged report tests.

Invariants locked here:
  * QC-PLACE-001..005 fire on the exact geometric defects they name, on the
    fields ingest already provides, with configurable mm tolerances.
  * A geometrically clean model produces no QC-PLACE findings (tolerances
    absorb sub-centimetre noise).
  * generate_report_bundle(stages=...) writes compliance_result.json with the three
    layers, renders schema/quality sections in the HTML, keeps unanchored quality findings in JSON while BCF skips them, and the overall status obeys stage precedence
    (schema failed > FAIL > review > not-evaluated > quality alerts > clean).
"""
from __future__ import annotations

import json
import os
import zipfile

from tests.helpers import run_quality_checks
from reporting.generator import generate_report_bundle
from reporting.report_model import OverallCode, compute_overall_status


def _bim(walls=(), doors=(), windows=(), **kw):
    base = {
        "rooms": [], "walls": list(walls), "doors": list(doors),
        "windows": list(windows),
        "_review_summary": {"threshold": 0.5, "flagged": [],
                            "scale_flagged": False, "scale_confidence": None},
    }
    base.update(kw)
    return base


def _wall(wid="W1", length=4000.0, thickness=200.0):
    return {"id": wid, "start_point": [0.0, 0.0, 0.0],
            "end_point": [length, 0.0, 0.0], "thickness": thickness}


def _codes(stage):
    # Phase 4 adds independent Room/Property/Unit/Storey plugins. These tests
    # intentionally isolate the QC-PLACE contract.
    return sorted(f["code"] for f in stage["findings"]
                  if str(f["code"]).startswith("QC-PLACE"))


# ── QC-PLACE ─────────────────────────────────────────────────────────────────

def test_clean_geometry_yields_no_place_findings():
    bim = _bim(walls=[_wall()],
               doors=[{"id": "D1", "host_wall_id": "W1", "width": 900.0,
                       "insertion_point": [1000.0, 0.0, 0.0]}])
    assert _codes(run_quality_checks(bim)) == []


def test_missing_host_is_place_001():
    bim = _bim(doors=[{"id": "D1", "host_wall_id": None, "width": 900.0,
                       "insertion_point": [0.0, 0.0, 0.0]}])
    assert _codes(run_quality_checks(bim)) == ["QC-PLACE-001"]


def test_dangling_host_is_place_002():
    bim = _bim(walls=[_wall("W1")],
               windows=[{"id": "N1", "host_wall_id": "W99", "width": 1200.0,
                         "insertion_point": [0.0, 0.0, 0.0]}])
    assert _codes(run_quality_checks(bim)) == ["QC-PLACE-002"]


def test_opening_wider_than_wall_is_place_003():
    bim = _bim(walls=[_wall("W1", length=800.0)],
               doors=[{"id": "D1", "host_wall_id": "W1", "width": 900.0,
                       "insertion_point": [400.0, 0.0, 0.0]}])
    stage = run_quality_checks(bim)
    assert "QC-PLACE-003" in _codes(stage)
    f = [x for x in stage["findings"] if x["code"] == "QC-PLACE-003"][0]
    assert f["element_id"] == "D1" and "800" in f["expected"]


def test_off_axis_insertion_is_place_004():
    bim = _bim(walls=[_wall("W1", thickness=200.0)],
               doors=[{"id": "D1", "host_wall_id": "W1", "width": 900.0,
                       "insertion_point": [1000.0, 500.0, 0.0]}])  # 500 mm off
    assert "QC-PLACE-004" in _codes(run_quality_checks(bim))


def test_near_axis_insertion_within_tolerance_is_clean():
    # 120 mm off a 200 mm wall: allowed = 100 + 50 tol = 150 → clean
    bim = _bim(walls=[_wall("W1", thickness=200.0)],
               doors=[{"id": "D1", "host_wall_id": "W1", "width": 900.0,
                       "insertion_point": [1000.0, 120.0, 0.0]}])
    assert _codes(run_quality_checks(bim)) == []


def test_combined_widths_exceeding_wall_is_place_005():
    bim = _bim(walls=[_wall("W1", length=2000.0)],
               doors=[{"id": "D1", "host_wall_id": "W1", "width": 900.0,
                       "insertion_point": [500.0, 0.0, 0.0]},
                      {"id": "D2", "host_wall_id": "W1", "width": 900.0,
                       "insertion_point": [1500.0, 0.0, 0.0]}],
               windows=[{"id": "N1", "host_wall_id": "W1", "width": 1200.0,
                         "insertion_point": [1000.0, 0.0, 0.0]}])
    stage = run_quality_checks(bim)
    codes = _codes(stage)
    assert "QC-PLACE-005" in codes
    f = [x for x in stage["findings"] if x["code"] == "QC-PLACE-005"][0]
    assert f["element_id"] == "W1"          # anchored on the wall
    assert "3000" in f["actual"]


def test_tolerances_overridable_per_run():
    bim = _bim(walls=[_wall("W1", length=880.0)],
               doors=[{"id": "D1", "host_wall_id": "W1", "width": 900.0,
                       "insertion_point": [400.0, 0.0, 0.0]}],
               _qc_tolerances={"place_len_tol_mm": 50.0})
    assert "QC-PLACE-003" not in _codes(run_quality_checks(bim))


# ── staged report ────────────────────────────────────────────────────────────

_SCHEMA = {"stage": "schema", "status": "passed", "schema": "IFC4",
           "findings": []}
_QUALITY = {"stage": "quality", "status": "passed_with_alerts",
            "findings": [{"finding_id": "abc123def456", "category": "quality",
                          "code": "QC-PLACE-003", "article_id": "QC-PLACE-003",
                          "verdict": "NOT_EVALUATED",
                          "message": "door D1 wider than wall",
                          "element_id": "D1", "expected": "<= 800 mm",
                          "actual": "900 mm"}]}


def _result():
    return {"summary": {"PASS": 1, "FAIL": 0, "NEEDS_REVIEW": 0,
                        "NOT_EVALUATED": 0},
            "duration_s": 0.1, "by_agent": {},
            "findings": [{"finding_id": "0" * 12, "category": "compliance",
                          "code": None, "article_id": "4-5-1",
                          "verdict": "PASS", "message": "ok",
                          "element_id": "R1"}]}


def test_staged_json_artifact_has_three_layers(tmp_path):
    paths = generate_report_bundle(_result(), {"plan_name": "t"},
                             output_dir=str(tmp_path), coverage=None,
                             stages={"schema": _SCHEMA, "quality": _QUALITY})
    staged = json.load(open(paths["json"], encoding="utf-8"))
    assert set(staged["stages"]) == {"schema", "quality", "compliance"}
    assert staged["stages"]["schema"]["status"] == "passed"
    assert staged["stages"]["quality"]["findings"][0]["code"] == "QC-PLACE-003"
    assert staged["stages"]["compliance"]["summary"]["PASS"] == 1


def test_html_renders_stage_sections(tmp_path):
    paths = generate_report_bundle(_result(), {"plan_name": "t"},
                             output_dir=str(tmp_path),
                             stages={"schema": _SCHEMA, "quality": _QUALITY})
    h = open(paths["html"], encoding="utf-8").read()
    assert "stage 1 — ifc schema check" in h
    assert "stage 2 — model quality check" in h
    assert "stage 3 — mabhas compliance check" in h
    assert "QC-PLACE-003" in h


def test_unanchored_quality_finding_stays_in_report_and_is_skipped_by_bcf(tmp_path):
    paths = generate_report_bundle(_result(), {"plan_name": "t"},
                             output_dir=str(tmp_path),
                             stages={"schema": _SCHEMA, "quality": _QUALITY})
    with zipfile.ZipFile(paths["bcf"]) as archive:
        assert not any(name.endswith("markup.bcf") for name in archive.namelist())
    staged = json.load(open(paths["json"], encoding="utf-8"))
    assert any(finding.get("code") == "QC-PLACE-003" for finding in staged["findings"])
    manifest = json.load(open(paths["bcf"] + ".manifest.json", encoding="utf-8"))
    assert manifest["topics_total"] == 0
    assert any("no trustworthy IFC GlobalId" in row["reason"]
               for row in manifest["skipped"])


def test_no_stages_keeps_backward_compatible_output(tmp_path):
    paths = generate_report_bundle(_result(), {"plan_name": "t"},
                             output_dir=str(tmp_path))
    h = open(paths["html"], encoding="utf-8").read()
    assert "stage 1 — ifc schema check" not in h
    assert os.path.exists(paths["json"])    # JSON always written


def test_overall_status_stage_precedence():
    failed_schema = {"stage": "schema", "status": "failed", "findings": []}
    failed_compliance = {"stage": "compliance", "status": "completed", "findings": [
        {"finding_id": "f", "stage": "compliance", "category": "compliance", "severity": "fail", "verdict": "FAIL"}
    ]}
    status = compute_overall_status(mode="full_check", stages={"schema": failed_schema, "quality": None, "compliance": failed_compliance})
    assert status.code is OverallCode.REJECTED

    passing = {"stage": "compliance", "status": "completed", "findings": [
        {"finding_id": "p", "stage": "compliance", "category": "compliance", "severity": "info", "verdict": "PASS"}
    ]}
    status = compute_overall_status(mode="full_check", stages={"schema": _SCHEMA, "quality": _QUALITY, "compliance": passing})
    assert status.code is OverallCode.COMPLIANT_WITH_QUALITY_ALERTS
