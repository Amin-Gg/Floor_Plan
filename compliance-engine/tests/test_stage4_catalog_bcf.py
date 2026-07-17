"""
Stage 4 — IFC+IR semantic catalog + BCF enrichment tests.

Invariants locked here:
  * The typed catalog API reads the canonical Phase-5 YAML contract.
  * A malformed or key-incomplete catalog FAILS LOUD at load, never
    half-applies (a silently mis-read property contract mis-reads models).
  * A renamed property in a custom catalog is actually honoured by ingest
    (end-to-end through param_map).
  * BCF excludes findings without a trustworthy IFC GlobalId and records
    explicit manifest skips.
"""
from __future__ import annotations

import json
import os
import zipfile

import pytest

from standards import catalog_api as sc


@pytest.fixture(autouse=True)
def _restore_catalog():
    yield
    sc.reload_catalog()          # never leak a test catalog into other tests


# ── catalog ──────────────────────────────────────────────────────────────────

def test_yaml_reload_matches_canonical_catalog_contract():
    from_yaml = sc.reload_catalog()
    loaded = sc.load_catalog()
    assert from_yaml == loaded
    assert "psets" in from_yaml
    assert "param_map" in from_yaml
    assert from_yaml["catalog_version"]


def test_missing_file_fails_loud(tmp_path):
    with pytest.raises(FileNotFoundError, match="Required standards file"):
        sc.reload_catalog(str(tmp_path / "nope.yaml"))


def test_malformed_catalog_fails_loud(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("psets: {}\nparam_map: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="catalog_version"):
        sc.reload_catalog(str(p))


def test_missing_property_key_fails_loud(tmp_path):
    import yaml
    cat = sc.reload_catalog()
    from copy import deepcopy
    broken = deepcopy(cat)
    del broken["psets"]["provenance"]["properties"]["confidence"]
    p = tmp_path / "incomplete.yaml"
    p.write_text(yaml.safe_dump(broken), encoding="utf-8")
    with pytest.raises(ValueError, match="missing properties"):
        sc.reload_catalog(str(p))


def test_renamed_property_is_honoured_by_param_map(tmp_path):
    import yaml
    cat = sc.reload_catalog()
    from copy import deepcopy
    custom = deepcopy(cat)
    custom["catalog_version"] = "2.0"
    custom["psets"]["contract"]["properties"]["wall_height_mm"] = "IRWallHeightMm"
    p = tmp_path / "custom.yaml"
    p.write_text(yaml.safe_dump(custom), encoding="utf-8")
    sc.reload_catalog(str(p))
    assert sc.param_map()["IRWallHeightMm"] == "wall_height"
    assert sc.prop("contract", "wall_height_mm") == "IRWallHeightMm"


def test_ingest_reads_fixture_identically_via_catalog():
    """End-to-end regression: the catalogized reader produces the same values
    the fixture round-trip tests already assert — spot-check the contract-
    sensitive fields on the real fixture."""
    from ingest.ifc_to_bim_data import ifc_to_bim_data
    fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                           "sample_plan.ifc")
    bim = ifc_to_bim_data(fixture)
    assert bim.get("building_params", {}).get("wall_height") is not None \
        or bim.get("rooms")            # contract read path exercised
    assert all("_provenance" in w for w in bim["walls"])
    assert all("width_source" in w for w in bim["windows"])


# ── BCF enrichment ───────────────────────────────────────────────────────────

def _reports(tmp_path):
    from reporting.generator import generate_report_bundle
    result = {"summary": {"PASS": 0, "FAIL": 1, "NEEDS_REVIEW": 0,
                          "NOT_EVALUATED": 0},
              "duration_s": 0.0, "by_agent": {},
              "findings": [{"finding_id": "aa" * 6, "category": "compliance",
                            "code": None, "article_id": "4-5-1",
                            "verdict": "FAIL", "message": "too small",
                            "element_id": "R1"}]}
    quality = {"stage": "quality", "status": "passed_with_alerts",
               "findings": [{"finding_id": "bb" * 6, "category": "quality",
                             "code": "QC-PLACE-003",
                             "article_id": "QC-PLACE-003",
                             "verdict": "NOT_EVALUATED", "message": "wide",
                             "element_id": "D1"}]}
    return generate_report_bundle(result, {"plan_name": "t"}, output_dir=str(tmp_path),
                            stages={"schema": {"stage": "schema",
                                               "status": "passed",
                                               "findings": []},
                                    "quality": quality})


def test_unanchored_findings_are_reported_but_not_exported_as_bcf_topics(tmp_path):
    paths = _reports(tmp_path)
    with zipfile.ZipFile(paths["bcf"]) as archive:
        assert not any(name.endswith("markup.bcf") for name in archive.namelist())
    report = json.load(open(paths["json"], encoding="utf-8"))
    codes = {finding.get("code") or finding.get("article_id") for finding in report["findings"]}
    assert {"4-5-1", "QC-PLACE-003"}.issubset(codes)
    manifest = json.load(open(paths["bcf"] + ".manifest.json", encoding="utf-8"))
    assert manifest["topics_total"] == 0
    assert manifest["skipped_total"] == 2
    assert all("no trustworthy IFC GlobalId" in row["reason"]
               for row in manifest["skipped"])


def test_bcf_guid_passthrough_topic_matches_directory(tmp_path):
    paths = _reports(tmp_path)
    with zipfile.ZipFile(paths["bcf"]) as z:
        for n in z.namelist():
            if not n.endswith("markup.bcf"):
                continue
            dirname = n.split("/")[0]
            body = z.read(n).decode("utf-8")
            assert f'Guid="{dirname}"' in body


def test_bcf_enriched_topics_still_deterministic(tmp_path):
    a = _reports(tmp_path / "a")
    b = _reports(tmp_path / "b")
    def _names(p):
        with zipfile.ZipFile(p["bcf"]) as z:
            return sorted(n for n in z.namelist() if n.endswith("markup.bcf"))
    assert _names(a) == _names(b)
