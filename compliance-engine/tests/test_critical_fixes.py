"""
tests/test_critical_fixes.py
============================
Regression tests for the 2026-07 review fixes. Purely ADDITIVE — no existing
test file is modified.

Covers:
  C2  rag.groq_client imports (and the whole rag stack behind it) without the
      groq package installed — verified in a clean subprocess interpreter.
  C3  Room min-width / max-length are orientation-independent: a room whose
      long axis runs along Y must FAIL a min-width rule its short side
      violates (previously a verified false PASS). Single-dimension rooms →
      NEEDS_REVIEW, never a guess.
  H1  Missing floor area is None, never 0.0: numeric area rules → NEEDS_REVIEW
      (not FAIL "area = 0.0"); glazing ratio of an unmeasured room is None and
      the opening agent routes it to NEEDS_REVIEW while a real measurable FAIL
      still wins.
  C4  api.pipeline LLM gating: disabled flag → None; no Groq keys → None;
      keys present → callable. run_pipeline end-to-end appends "[AI note:" to
      NEEDS_REVIEW findings when an llm is wired, and never touches PASS/FAIL.
  M3  One LLM call (and one retrieval) per CLAUSE, not per element finding.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "services")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.numeric_checker import NumericChecker, Verdict          # noqa: E402
from services.opening_agent import OpeningAgent                       # noqa: E402
from services.spatial_graph import SpatialGraph                       # noqa: E402
from services.orchestrator import _llm_review_interpretive            # noqa: E402
from services.numeric_checker import Finding                          # noqa: E402
from ingest.ifc_to_bim_data import _area_or_none, _bbox_dims_mm       # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# C3 — orientation-independent room dimensions
# ═══════════════════════════════════════════════════════════════════════════

_MIN_WIDTH_CLAUSE = {
    "article_id": "T-C3-width", "rule_type": "numeric",
    "text_en": "minimum room width 2.5 m",
    "entities": {"object": "bedroom", "property": "min width",
                 "comparator": ">=", "value": 2.5, "unit": "m",
                 "condition": None},
}


def _room(dims):
    return {"rooms": [{"id": "R1", "category": "room_bedroom",
                       "area_m2": 8.8, "dimensions": dims}],
            "doors": [], "windows": []}


def test_c3_y_long_room_fails_min_width():
    """The exact reproduced bug: short side 2.1 m stored under length_mm."""
    bim = _room({"width_mm": 4200, "length_mm": 2100})
    f = NumericChecker(bim).check_all([_MIN_WIDTH_CLAUSE])[0]
    assert f.verdict == Verdict.FAIL, f.message
    assert f.measured == 2.1  # the SHORT side was measured


def test_c3_x_long_room_unchanged():
    """Rooms already stored width<=length behave exactly as before."""
    bim = _room({"width_mm": 2100, "length_mm": 4200})
    f = NumericChecker(bim).check_all([_MIN_WIDTH_CLAUSE])[0]
    assert f.verdict == Verdict.FAIL and f.measured == 2.1


def test_c3_max_length_reads_longer_side_either_orientation():
    clause = {"article_id": "T-C3-len", "rule_type": "numeric",
              "text_en": "room length at most 4 m",
              "entities": {"object": "bedroom", "property": "length",
                           "comparator": "<=", "value": 4, "unit": "m",
                           "condition": None}}
    for dims in ({"width_mm": 4200, "length_mm": 2100},
                 {"width_mm": 2100, "length_mm": 4200}):
        f = NumericChecker(_room(dims)).check_all([clause])[0]
        assert f.verdict == Verdict.FAIL and f.measured == 4.2


def test_c3_single_dimension_is_never_guessed():
    """One dim present → cannot know if it is the short or long side."""
    bim = _room({"width_mm": 3000})
    f = NumericChecker(bim).check_all([_MIN_WIDTH_CLAUSE])[0]
    assert f.verdict == Verdict.NEEDS_REVIEW


def test_c3_ingest_bbox_orders_extents():
    tall = [[0, 0], [2100, 0], [2100, 4200], [0, 4200], [0, 0]]   # long in Y
    wide = [[0, 0], [4200, 0], [4200, 2100], [0, 2100], [0, 0]]   # long in X
    for poly in (tall, wide):
        d = _bbox_dims_mm(poly)
        assert d["width_mm"] == 2100.0 and d["length_mm"] == 4200.0
    assert _bbox_dims_mm([]) == {"length_mm": 0.0, "width_mm": 0.0}


# ═══════════════════════════════════════════════════════════════════════════
# H1 — missing area is None, never a measured 0.0
# ═══════════════════════════════════════════════════════════════════════════

def test_h1_area_or_none():
    assert _area_or_none(None) is None
    assert _area_or_none("") is None
    assert _area_or_none(0) is None
    assert _area_or_none(0.0) is None
    assert _area_or_none("garbage") is None
    assert _area_or_none(9.5) == 9.5
    assert _area_or_none("9.5") == 9.5


def test_h1_missing_area_is_review_not_fail():
    clause = {"article_id": "T-H1-area", "rule_type": "numeric",
              "text_en": "minimum bedroom area 6.5 m2",
              "entities": {"object": "bedroom", "property": "area",
                           "comparator": ">=", "value": 6.5, "unit": "m2",
                           "condition": None}}
    bim = {"rooms": [{"id": "R1", "category": "room_bedroom",
                      "area_m2": None,
                      "dimensions": {"width_mm": 3000, "length_mm": 3000}}],
           "doors": [], "windows": []}
    f = NumericChecker(bim).check_all([clause])[0]
    assert f.verdict == Verdict.NEEDS_REVIEW, (
        "an UNMEASURED room must not FAIL with 'area = 0.0'")


def _rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def _glazing_bim(bed_area):
    """Bedroom (area = bed_area, exterior 1.5×1.5 m window → 2.25 m² glazing)
    next to a kitchen (9 m², same window on the FAR perimeter wall → ratio
    0.25 passes >= 0.125). Windows sit on true perimeter walls so the
    footprint-based exterior detection classifies them correctly."""
    return {
        "walls": [
            {"id": "WL", "start_point": [0, 0, 0], "end_point": [0, 3000, 0],
             "thickness": 150, "is_exterior": True},
            {"id": "WR", "start_point": [6000, 0, 0], "end_point": [6000, 3000, 0],
             "thickness": 150, "is_exterior": True},
        ],
        "rooms": [
            {"id": "R_bed", "category": "room_bedroom", "area_m2": bed_area,
             "polygon": _rect(0, 0, 3000, 3000),
             "dimensions": {"width_mm": 3000, "length_mm": 3000}},
            {"id": "R_kit", "category": "room_kitchen", "area_m2": 9.0,
             "polygon": _rect(3000, 0, 6000, 3000),
             "dimensions": {"width_mm": 3000, "length_mm": 3000}},
        ],
        "doors": [], "stairs": [], "railings": [],
        "windows": [
            {"id": "Win_b", "host_wall_id": "WL", "insertion_point": [0, 1500, 0],
             "width": 1500, "height": 1500, "sill_height": 900},
            {"id": "Win_k", "host_wall_id": "WR", "insertion_point": [6000, 1500, 0],
             "width": 1500, "height": 1500, "sill_height": 900},
        ],
    }


_RATIO_CLAUSE = {
    "article_id": "T-H1-ratio", "rule_type": "numeric",
    "text_en": "glazing area at least 1/8 of floor area",
    "entities": {"object": "window_area", "property": "ratio_to_floor_area",
                 "comparator": ">=", "value": 0.125, "unit": "ratio",
                 "condition": None},
}


def test_h1_glazing_ratio_none_for_unmeasured_room():
    sg = SpatialGraph(_glazing_bim(bed_area=None))
    assert sg.glazing_ratio("R_bed") is None
    assert sg.glazing_ratio("R_kit") == pytest.approx(0.25)


def test_h1_unmeasured_room_downgrades_glazing_pass_to_review():
    """Kitchen passes; bedroom is unmeasured → clause must be NEEDS_REVIEW,
    not the old false FAIL (ratio 0.0) and not a false all-rooms PASS."""
    sg = SpatialGraph(_glazing_bim(bed_area=None))
    findings = OpeningAgent(sg).check_all([_RATIO_CLAUSE])
    f = [x for x in findings if x.article_id == "T-H1-ratio"][0]
    assert f.verdict == Verdict.NEEDS_REVIEW
    assert "R_bed" in f.message


def test_h1_measurable_fail_still_wins_over_unmeasured():
    """Deterministic FAIL is preserved: kitchen ratio shrunk below 0.125 by a
    big floor area; bedroom unmeasured. FAIL must win, review must not mask it."""
    bim = _glazing_bim(bed_area=None)
    bim["rooms"][1]["area_m2"] = 40.0          # kitchen ratio 2.25/40 ≈ 0.056
    findings = OpeningAgent(SpatialGraph(bim)).check_all([_RATIO_CLAUSE])
    f = [x for x in findings if x.article_id == "T-H1-ratio"][0]
    assert f.verdict == Verdict.FAIL
    assert "R_kit" in f.message


def test_h1_all_measured_glazing_pass_unchanged():
    sg = SpatialGraph(_glazing_bim(bed_area=9.0))
    findings = OpeningAgent(sg).check_all([_RATIO_CLAUSE])
    f = [x for x in findings if x.article_id == "T-H1-ratio"][0]
    assert f.verdict == Verdict.PASS


# ═══════════════════════════════════════════════════════════════════════════
# C2 — rag stack imports without the groq package (clean subprocess)
# ═══════════════════════════════════════════════════════════════════════════

def test_c2_rag_stack_imports_without_groq_package():
    script = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def block(name, *a, **k):\n"
        "    if name == 'groq' or name.startswith('groq.'):\n"
        "        raise ModuleNotFoundError(\"No module named 'groq'\")\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = block\n"
        "import rag.groq_client\n"
        "import rag.query_transforms\n"
        "import rag.rag_retriever\n"
        "print('OK')\n"
    )
    out = subprocess.run([sys.executable, "-c", script], cwd=_ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_c2_groq_call_still_requires_keys(monkeypatch):
    """The lazy import must not weaken the lazy KEY check: calling without
    keys still raises the explicit RuntimeError, not an import error."""
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    import rag.groq_client as gc
    monkeypatch.setattr(gc, "_KEYS", None)     # reset the lazy cache
    with pytest.raises(RuntimeError, match="GROQ_API_KEYS"):
        gc.groq_chat(messages=[{"role": "user", "content": "hi"}])


# ═══════════════════════════════════════════════════════════════════════════
# M3 — one LLM call and one retrieval per CLAUSE, fanned out per element
# ═══════════════════════════════════════════════════════════════════════════

class _CountingLLM:
    def __init__(self, reply="Check the sill height on site."):
        self.calls, self.reply, self.prompts = 0, reply, []

    def __call__(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        return self.reply


class _CountingRetriever:
    def __init__(self):
        self.calls = 0

    def retrieve(self, query, top_k=3, **kw):
        self.calls += 1
        return [{"article_id": "4-5-1", "text_en": "Context clause text."}]


def _findings_three_reviews_one_pass():
    mk = lambda i: Finding(article_id="4-5-1", verdict=Verdict.NEEDS_REVIEW,
                           message=f"win_{i}: sill unknown — needs review",
                           element_id=f"win_{i}", rule_text_en="sill rule")
    passed = Finding(article_id="4-9-9", verdict=Verdict.PASS,
                     message="door ok", rule_text_en="door rule")
    return [mk(1), mk(2), mk(3), passed]


def test_m3_one_llm_and_one_retrieval_per_clause():
    findings = _findings_three_reviews_one_pass()
    llm, rt = _CountingLLM(), _CountingRetriever()
    clauses_by_id = {"4-5-1": {"article_id": "4-5-1", "text_en": "sill rule"}}
    _llm_review_interpretive(findings, clauses_by_id, rt, llm)

    assert llm.calls == 1, "3 element findings of one clause → exactly 1 LLM call"
    assert rt.calls == 1, "…and exactly 1 retrieval"
    for f in findings[:3]:
        assert "[AI note: Check the sill height on site.]" in f.message
        assert f.verdict == Verdict.NEEDS_REVIEW          # verdict untouched
    assert "[AI note:" not in findings[3].message          # PASS untouched
    assert findings[3].verdict == Verdict.PASS
    # the single prompt aggregates the element reasons
    assert "3 element(s)" in llm.prompts[0]


def test_m3_two_clauses_two_calls():
    findings = _findings_three_reviews_one_pass()
    findings.append(Finding(article_id="4-7-2", verdict=Verdict.NEEDS_REVIEW,
                            message="corridor unmapped — needs review",
                            rule_text_en="corridor rule"))
    llm = _CountingLLM()
    _llm_review_interpretive(findings, {}, None, llm)
    assert llm.calls == 2


def test_m3_llm_failure_never_breaks_findings():
    findings = _findings_three_reviews_one_pass()

    def boom(prompt):
        raise RuntimeError("All 9 Groq keys returned 429")

    _llm_review_interpretive(findings, {}, None, boom)
    assert all("[AI note:" not in f.message for f in findings)
    assert findings[0].verdict == Verdict.NEEDS_REVIEW


def test_m3_no_llm_is_a_noop():
    findings = _findings_three_reviews_one_pass()
    before = [f.message for f in findings]
    _llm_review_interpretive(findings, {}, _CountingRetriever(), None)
    assert [f.message for f in findings] == before


# ═══════════════════════════════════════════════════════════════════════════
# C4 — production wiring and gating in api.pipeline
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def _fresh_wiring(monkeypatch):
    import api.pipeline as ap
    ap.reset_llm_wiring_for_tests()
    yield ap
    ap.reset_llm_wiring_for_tests()


def test_c4_disabled_flag_returns_none(_fresh_wiring, monkeypatch):
    monkeypatch.setenv("LLM_PASS_ENABLED", "0")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")
    assert _fresh_wiring._get_llm() is None


def test_c4_no_keys_returns_none(_fresh_wiring, monkeypatch):
    monkeypatch.setenv("LLM_PASS_ENABLED", "1")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    for var in ("GROQ_API_KEYS", "GROQ_API_KEY",
                "AGENTROUTER_API_KEY", "AGENT_ROUTER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert _fresh_wiring._get_llm() is None


def test_c4_keys_present_returns_callable_and_caches(_fresh_wiring, monkeypatch):
    monkeypatch.setenv("LLM_PASS_ENABLED", "1")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("AGENTROUTER_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_ROUTER_TOKEN", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dummy")
    llm = _fresh_wiring._get_llm()
    assert callable(llm)                       # built, NOT called (dummy key)
    assert _fresh_wiring._get_llm() is llm     # cached across jobs


def test_c4_run_pipeline_end_to_end_appends_ai_note(_fresh_wiring, monkeypatch,
                                                    tmp_path):
    """Full production entry point: an interpretive clause must reach the
    report with an [AI note: …] when an llm is wired — and PASS/FAIL findings
    must be byte-identical to the no-LLM run."""
    ap = _fresh_wiring
    llm = _CountingLLM(reply="Reviewer should verify on site.")
    monkeypatch.setattr(ap, "_get_llm", lambda: llm)
    monkeypatch.setattr(ap, "_get_retriever", lambda: _CountingRetriever())

    bim = {"rooms": [{"id": "R1", "category": "room_bedroom", "area_m2": 9.0,
                      "dimensions": {"width_mm": 3000, "length_mm": 3000}}],
           "doors": [], "windows": []}
    clauses = [
        {"article_id": "A-pass", "rule_type": "numeric",
         "text_en": "min bedroom area 6 m2",
         "entities": {"object": "bedroom", "property": "area",
                      "comparator": ">=", "value": 6, "unit": "m2",
                      "condition": None}},
        {"article_id": "A-interp", "rule_type": "numeric",
         "text_en": "corridor width shall be adequate",
         "entities": {"object": "corridor", "property": "width",
                      "comparator": ">=", "value": 1.1, "unit": "m",
                      "condition": None}},   # 'corridor' unmapped → review
    ]
    out = ap.run_pipeline(bim, clauses, out_dir=str(tmp_path))

    assert llm.calls == 1
    assert out["summary"]["PASS"] == 1 and out["summary"]["NEEDS_REVIEW"] == 1
    html = (tmp_path / "compliance_report.html").read_text(encoding="utf-8")
    assert "[AI note: Reviewer should verify on site.]" in html
    # PASS finding must carry no AI note
    assert "A-pass" in html and html.count("[AI note:") == 1