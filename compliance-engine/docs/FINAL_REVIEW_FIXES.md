> **Superseded by Final R2.** This document records the first review-fix patch.
> Final R2 additionally removes the direct compliance bypass, defines enriched
> `bim_data` as output-only, and requires a real IFC GlobalId for every BCF
> topic. See `docs/FINAL_R2_CLOSURE.md`.

# Final Review Fixes — Changes Applied After the Phase 0–9 Independent Review

**Base:** `compliance-engine-final-phase9.zip` (tag `stage8-phase9-final`, commit `fc1c6f4`)
**Result:** `compliance-engine-final-reviewfixes.zip`
**Test evidence:** 546 passed / 0 failed / 0 errors / 0 skipped, 0 `PytestUnraisableExceptionWarning`
(`docs/test-results/final_review_fixes_full.xml`; baseline was 530 — 16 new tests added)

This document lists every change made in response to the final independent
review, in review-finding order. No deterministic verdict changed:
the reference IFC + full corpus still produce exactly
`PASS 15 / FAIL 9 / NEEDS_REVIEW 309 / NOT_EVALUATED 8`, and entry-point
equivalence (IFC vs bim_data) was re-verified after the fixes.

---

## Fix 1 (High) — Removed flat `building_params` was half-alive

### What the review found
The Phase 9 changelog claimed the removed flat `building_params` input is
"explicitly rejected with a migration message." That was true only for the
`/analyze-ifc` form field. A flat block embedded inside `bim_data` was:

- **an unvalidated verdict-driving bypass** for keys the deterministic agents
  consume — proven by probe: `building_params: {"ceiling_height_mm": 2900}`
  flipped clause H1 (min ceiling height) from `NOT_EVALUATED` to `PASS`
  while skipping every Manual-Inputs-v1 validation (types, ranges,
  cross-field rules, provenance);
- **a silent no-op** for keys nothing consumes — old Stage-8 clients got
  plausible reports in which their supplied values never applied.

Both violate the remediation hard rules (no silent behavior change; no
provenance-free values driving verdicts).

### What changed
| File | Change |
|---|---|
| `manual_inputs/legacy_guard.py` | **New.** Single authority: `reject_legacy_building_params(bim_data)` raises `ManualInputsError` with the migration message and the offending keys. Empty/marker-only legacy blocks remain accepted because they contain no
verdict-driving values. Value-bearing enriched seams are output-only and are
rejected if resubmitted as public raw input. Non-mapping blocks are rejected too. |
| `manual_inputs/__init__.py` | Exports `reject_legacy_building_params`, `legacy_building_params_keys`, `LEGACY_BUILDING_PARAMS_MESSAGE`. |
| `services/validation_pipeline.py` | Guard invoked at `bim_data` ingestion in `_load_source`, before model construction — every channel (CLI, Celery, direct calls) now fails loudly, not just HTTP. |
| `api/main.py` | `/analyze` pre-validates at the boundary and returns **400** with the same migration message as `/analyze-ifc`, instead of queuing a doomed or silently-wrong job. |
| `tests/pipeline/test_legacy_building_params_guard.py` | **New, 9 tests.** Pins: guard message + offending keys; marker-only blocks accepted (`None`, `{}`, `{"_provided": []}`, `{"_provided": [...]}`); non-mapping rejected; the exact H1 bypass now raises; the internal marker path still yields `NOT_EVALUATED` (no unvalidated value applied); v1 `manual_inputs` remains the working path (H1 → `PASS`); `/analyze` returns 400 with the message; marker-only block passes the endpoint (using the codebase's `submit_job` stub convention). |

### Why this design
`building_params` inside `bim_data` is now, by Phase 9's own architecture, an
*internal output seam* of the manual merge — never a public input. Rejection
therefore happens at input boundaries only; internal seams produced *by* the
pipeline (which legitimately contain resolved values) are outputs and are
never passed through the guard. Callers constructing a typed `BuildingModel`
programmatically retain `model.parameters` as a typed internal API.

---

## Fix 2 (Medium) — Geometry-fallback identity now raises a Quality alert

### What the review found
ADR-001 mandates: using the deterministic geometry-fallback identity (element
with neither IFC `GlobalId` nor source ID) must emit a Quality alert, because
such elements cannot be targeted by manual-input element overrides or BCF
component selection, and a geometrically identical unidentified element would
collide on the same internal ID. The flag `used_geometry_fallback` was set in
Phase 1 but had **zero consumers** through Phase 9 — tracked since the Phase 1
review and never closed.

### What changed
| File | Change |
|---|---|
| `validation/quality/checks/identity_integrity.py` | **New plugin.** `IdentityIntegrityCheck` — `name="identity_integrity"`, `code_prefix="QC-IDENT"`, `codes=("QC-IDENT-001",)`, `blocking=False`. Scans all element collections; emits one alert per fallback-identified element with internal ID, expected/actual, and details. Non-blocking by design: a fallback identity degrades addressability, not the trustworthiness of the measurements the agents evaluate. |
| `validation/quality/checks/__init__.py`, `validation/quality/registry.py` | Plugin exported and registered second in the default ordered registry (after `contract_read` — identity problems are fundamental context for everything that follows). |
| `tests/validation/quality/test_identity_integrity.py` | **New, 5 tests.** Fallback element → exactly one `QC-IDENT-001` alert carrying the element's internal ID and no IFC GUID; identified elements → `applies_to` False and no findings; plugin registered and non-blocking; the alert alone leaves the stage at `passed_with_alerts`; **BCF invariant** — for a report containing the alert, the archive contains zero `IfcGuid` components (nothing fabricated), the finding is explicitly skipped from BCF and remains in JSON/HTML/PDF;
markup-only internal-reference topics are not allowed. |
| `tests/validation/quality/test_registry.py`, `tests/validation/quality/test_pipeline_uses_plugins.py` | The two registry-order documentation tests updated to the new canonical order (`identity_integrity` at position 2). These tests exist to pin the explicit order; adding a plugin legitimately updates the pin. |

No verdicts change: elements from IFC ingestion always carry GUIDs, and the
acceptance fixture's synthetic window carries a `source_id`, so the plugin is
inert on all reference runs (verified — parity summary unchanged).

---

## Fix 3 (Low) — ADR-002 amended to document the `ordinal` disambiguator

`docs/adr/ADR-002-unified-finding-contract.md`: the `finding_id` basis
specification now includes the implemented behavior — when Stage-8 agents emit
multiple findings with the same semantic basis, `assign_finding_ordinals()`
assigns a deterministic per-run ordinal and a **non-zero** ordinal is appended
to the basis as an additional `US`-separated component. Ordinal-0 findings
omit the component, so their IDs match the pre-amendment specification
exactly; this documents existing behavior and does not bump the `"v1"` token.
(Second unclosed tracked item from the Phase 1 review — now closed.)

---

## Fix 4 (Low) — Acceptance run now hard-requires the complete artifact set

### What the review found
The pipeline treats WeasyPrint as optional ("PDF generation skipped") while
the flagship acceptance test hard-asserts the PDF exists — so the release-gate
test failed on any machine without pango/cairo, with a bare `assert` and no
explanation.

### What changed
`scripts/run_validation_acceptance.py`: after report generation, the run
verifies all four required artifacts (`json`, `html`, `pdf`, `bcf`) and raises
a `RuntimeError` naming the missing kinds with the remedy ("install WeasyPrint
and its system libraries (pango/cairo) — see requirements.txt and the
Dockerfile"). Ordinary pipeline runs keep degrading gracefully; the
*acceptance* run — the release gate whose deliverable set municipalities
receive — fails loudly and self-diagnosing. The runtime-optional /
acceptance-mandatory policy split is now explicit and intentional.

---

## Fix 5 (Docs) — Acceptance command references

`README.md` and `docs/PHASE9_ACCEPTANCE.md` already used the correct command
(`--ifc tests/fixtures/sample_plan.ifc`); no repo change was needed. The
execution roadmap (`STAGE8_REMEDIATION_ROADMAP.md`, task 9.4) referenced the
planning-stage placeholder `remediation_acceptance.ifc` and was corrected to
the real command and the as-built fixture description (scenario derived from
the sample IFC with injected defects — the design that keeps real GlobalIds
available for BCF cross-checking).

---

## Verification performed after the fixes

| Check | Result |
|---|---|
| Full suite | **546 passed, 0 failed, 0 errors, 0 skipped** (530 baseline + 16 new) |
| `PytestUnraisableExceptionWarning` | 0 |
| Verdict parity (reference IFC + full corpus, unified pipeline) | `PASS 15 / FAIL 9 / NEEDS_REVIEW 309 / NOT_EVALUATED 8` — unchanged |
| H1 half-alive bypass probe | now raises `ManualInputsError` with the migration message |
| Entry-point equivalence (`ifc` vs `bim_data`) | identical compliance signatures — unchanged |
| One-command acceptance | all five artifacts produced; per-window overrides distinct (Wb = 1400 mm, W-ACCEPT-02 = 900 mm); `ok: true` |
| BCF invariant with QC-IDENT alerts present | zero fabricated `IfcGuid` components |

## Follow-ups for your local repository

The fixes are code + docs only; your git history, tag, and delivery checksums
must be regenerated on your machine:

```bash
git checkout -b remediation/final-review-fixes stage8-phase9-final
# unzip compliance-engine-final-reviewfixes.zip over the repo root
git add -A
git commit -m "Final review fixes: close half-alive building_params path (400 + \
pipeline guard), QC-IDENT-001 geometry-fallback alert, ADR-002 ordinal \
amendment, acceptance artifact hard-check"
git tag stage8-phase9-final-r2
pytest -q                        # expect 546 passed
python -m scripts.run_validation_acceptance \
  --ifc tests/fixtures/sample_plan.ifc \
  --manual-inputs tests/fixtures/remediation_manual_inputs.json \
  --output-dir artifacts/remediation_acceptance
# regenerate _delivery: SHA256SUMS, git bundle, GIT_EVIDENCE, junit copies
```

Remaining known non-blockers (unchanged from the review, tracked):
`Window(Door)` inheritance cleanup (deliberately deferred); BCF desktop GUI
viewer import remains a documented manual external check; the 309
`NEEDS_REVIEW` clause mass is the next product (not architecture) frontier.
