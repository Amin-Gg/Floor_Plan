# CHANGES — Geometry-Reliability Pass (Issues 5, 6, 7) + 10, 12, 14

This pass implements the deferred geometry-reliability work from the risk-review
and closes the remaining actionable plan items. As before, all edits are
surgical/additive and **no deterministic verdict logic** (the 4 agents /
SpatialGraph / `run_compliance`) was modified. The key property: every new
confidence flag travels all the way to `Pset_SimsysProvenance` so the compliance
engine actually **downgrades** verdicts on uncertain elements — the flags are not
cosmetic.

## Issue 5 — room-extraction confidence / suspicious-room flags

- `analysis/room_analysis.py`
  - New `_assess_room_quality(...)` → `(confidence, needs_review, review_reasons)`.
    Flags: implausible area (outside 1.5–120 m²), thin/sliver polygon
    (aspect > 8:1), image-border contact (within 1% of an edge ⇒ possibly
    cropped), missing OCR type (no label matched), degenerate polygon (< 4 verts).
  - Every extracted room now carries `confidence`, `needs_review`,
    `review_reasons` (in addition to the existing `name_source`).
- `export/ifc_exporter.py` — the room provenance now writes the room's
  `confidence` and the real `review_reasons` (was a hardcoded string).

## Issue 6 — host-wall binding scoring

- `analysis/room_analysis.py` — new `assess_host_wall(insertion_point, walls)`
  returning `{host_wall_id, host_wall_confidence, host_wall_distance_mm,
  candidate_host_walls, needs_review, review_reason}`. Confidence falls with
  distance (0 mm→1.0, 600 mm→0.0); two near-equidistant walls ⇒ ambiguous host
  (penalised + flagged); > 1000 mm ⇒ no host. `find_host_wall_id` is unchanged
  (kept for back-compat).
- `routes/visualization_routes.py` — doors/windows now bind via
  `assess_host_wall` and carry the binding fields.
- `services/bim_builder.py` — `_build_doors` / `_build_windows` propagate the
  binding fields and set per-opening `confidence` / `needs_review` / `review_reason`.
- `export/ifc_exporter.py` — door/window provenance writes these.

## Issue 7 — exterior-classification confidence

- `analysis/wall_analysis.py` — `identify_exterior_walls` now adds
  `exterior_confidence` + `exterior_needs_review` (strong boundary evidence →
  high; "unconnected" alone → 0.4, flagged) alongside the existing
  `exterior_reasons`.
- `services/bim_builder.py` — `_build_walls` carries `exterior_confidence` /
  `exterior_reasons`; a **window on a low-confidence-exterior host wall inherits
  review** (its confidence is dragged down and it is flagged) so natural-light /
  ventilation rules can't hard-PASS/FAIL on an uncertain exterior.
- `export/ifc_exporter.py` — wall/window provenance writes these.

## Issue 14 — pytest collection no longer crashes

- `tests/test_room_binding.py` — was a script that called `sys.exit()` at import,
  which crashed `pytest` collection (`INTERNALERROR: SystemExit`). Refactored to
  clean pytest: `importorskip` for opencv, module-level `pytest.skip` on import
  failure, a module-scoped fixture, self-contained `test_*` functions, and a
  `__main__` guard. The whole `tests/` directory now collects (57 tests) without
  side effects.
- `tests/test_geometry_confidence.py` *(new)* — 10 tests covering Issues 5/6/7 and
  builder propagation (tiny/sliver/cropped/untyped rooms; missing/ambiguous/far
  host walls; strong-vs-weak exterior; window inheriting weak-exterior review).

## Issue 10 — RAG honesty (documentation + report)

- The engine README already stated the design honestly ("deterministic spine, AI
  on the wings"; verdicts never from an LLM). This pass adds an explicit line to
  the **report methodology footer**: retrieval (RAG) and any LLM step supply
  supporting clause context and advisory notes on review items only — they never
  produce or change a PASS/FAIL verdict. (RAG failure already cannot change
  verdict quality — verified in the honesty pass.)

## Issue 12 — independent deployability (verified)

Section 1 (root) and Section 2 (`compliance-engine/`) each ship their own
`Dockerfile` and `requirements.txt`, share no global state, and communicate only
through the IFC file. No code change needed; confirmed present.

## Verified (not just compiled)

- All three heuristics behave correctly on crafted inputs (good vs
  tiny/sliver/cropped/untyped rooms; close/ambiguous/far host walls;
  strong/weak exterior).
- **End-to-end:** a flagged room (conf 0.4) and window (conf 0.3) → Section-1 IFC
  export (passes the §A7 gate) → engine loads the provenance (conf 0.4 / 0.3,
  `needs_review=True`) → both elements flagged → **4 dependent verdicts
  downgraded** to NEEDS_REVIEW, and no hard PASS/FAIL remains on them.
- Section-1 suite: `test_room_binding.py` (5) + `test_geometry_confidence.py` (10)
  pass; the full `tests/` directory collects cleanly (57 tests).
- Engine suite (normalizer, coverage, clause-health, round-trip, deterministic
  regression) remains green.

## Full issue status (risk-review, all 16)

| # | Issue | Status |
|---|-------|--------|
| 1  | Detector-scope honesty | ✅ docs honest (4-class) |
| 2  | Room taxonomy mismatch | ✅ canonical `room_*` both sides |
| 3  | IFC-based flow / `/analyze-ifc` | ✅ engine endpoint + CLI |
| 4  | Scale calibration + confidence | ✅ source/confidence + engine gate |
| 5  | Room-extraction robustness | ✅ **this pass** (confidence + flags) |
| 6  | Host-wall binding | ✅ **this pass** (scoring + candidates) |
| 7  | Exterior classification | ✅ **this pass** (confidence + inherit) |
| 8  | Coverage honesty | ✅ coverage table + UNSUPPORTED |
| 9  | Clause-corpus health | ✅ fail-fast / degraded |
| 10 | RAG honesty | ✅ docs + report footer |
| 11 | Provenance | ✅ Psets round-trip |
| 12 | Split containers | ✅ separate Docker/reqs (verified) |
| 13 | YOLO wiring honesty | ✅ documented not-wired |
| 14 | Pytest refactor | ✅ **this pass** |
| 15 | Unit accuracy | ✅ m² / mm consistent |
| 16 | Versioned schema | ✅ `bim-canonical-v1` |

## Genuinely out of scope (cannot be built here)

- **Interactive scale-calibration UI** and a **learned room-segmentation model**
  (the Issue 4/5 "future" options). These require a front-end and model training
  that aren't possible in this environment. The plan frames them as future work;
  the confidence + review flags shipped here are the prescribed interim fix and
  keep the system honest until then.
