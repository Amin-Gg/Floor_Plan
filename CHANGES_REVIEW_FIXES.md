# CHANGES — Review Fixes (Section 1 / Mask R-CNN)

This log records the **Critical + High** review items fixed in this checkout.
All edits are **surgical and additive** — no existing logic or Mask2Former-future
code was deleted. The supplementary **YOLO** integration (H3) was intentionally
**deferred to Section 2**, per the project plan.

Verification: every changed `.py` file compiles (`py_compile` clean).

---

## C1 — IFC dependency was uninstallable; re-pinned to the 0.8.x line
**File:** `requirements.txt`
**Before:** `ifcopenshell==0.7.0` (does not exist on PyPI → `pip install` aborted; the
exporter also uses the 0.8.x modular API, so 0.7.x would not run anyway).
**After:** `ifcopenshell>=0.8.0,<0.9`, with a comment and a post-install verify line.
**Why it matters:** This unblocks the `/export/ifc` endpoint — your actual 3D output.
The exporter already guards the `void`→`feature` rename across 0.8.x, so it is
compatible with this pin. **Action for you:** reinstall and run one `/export/ifc`
end-to-end to confirm on your machine.

## C2 — Weight-loader fallback made visible (downgraded from "silent no-op")
**File:** `mrcnn/model.py` (`load_weights`)
**Correction to the original review:** on closer reading, when `keras.engine.saving`
is unavailable (Keras 2.13), the loader **does** load weights via
`self.keras_model.load_weights(filepath, by_name=True, skip_mismatch=True)` — it is
*not* a total silent skip. That is consistent with your "it runs and outputs."
**Change:** added a clear `WARNING` log on that fallback path. The residual risk is
that `skip_mismatch=True` would hide a head-shape mismatch (wrong `NUM_CLASSES` vs the
`.h5`) by silently skipping just those layers; the warning makes the path diagnosable.
**Action for you:** if a `vis{N}.png` overlay ever looks random, check the log for this
warning and confirm `NUM_CLASSES == 4`.

## C3 — `weights/README.md` corrected to Mask R-CNN reality
**File:** `weights/README.md` (rewritten)
**Before:** claimed "uses Mask2Former, no `.h5` files, use safetensors" + referenced a
`train_mask2former.py` not in the repo → would send you to the wrong artifact.
**After:** documents the required `weights/maskrcnn_15_epochs.h5` (~244 MB, 4-class),
how to place it, and how to verify it loaded (`smoke_test.py`). Notes that `yolo_best.pt`
is Section 2 and not used yet.

## H1 — 4-class scope made explicit (no code removed)
**Files:** `readme.md` (rewritten), `config/classes.py` (banner added)
The running model detects **only wall / window / door**. The stair/room/slab/railing/
closet branches in `routes/visualization_routes.py` remain (kept for the Mask2Former
phase) but can never fire with a 4-class model, so those output arrays are always empty.
This is now stated plainly in the README so empty `stairs`/`slabs`/`ml_detections`
are not mistaken for "compliant."

## H2 — Untyped rooms flagged for review (additive fields)
**File:** `analysis/room_analysis.py`
Room type/name comes only from OCR. Added two **additive** fields to each room dict:
- `"name_source"`: `"ocr"` if a label was bound, else `"none"`
- `"needs_review"`: `true` when `name_source == "none"`
These flow through `bim_builder` unchanged and let the downstream code-checker route
untyped rooms to `NEEDS_REVIEW` instead of treating `category:"Unknown"` as checkable.
Existing consumers are unaffected (no fields removed/renamed).

## M1 — Root `readme.md` rewritten to the real project
Replaced the stale Mask2Former + Compliance-Engine description with an accurate
Section-1 description: pipeline, endpoints, quick-start, building parameters, honest
limitations, and the phase plan (Section 2 = YOLO; compliance engine = separate repo).

## M2 — `config/classes.py` labeled as not driving the live model
Added a banner clarifying the 15-class taxonomy is for the shelved Mask2Former path and
is **not** what the running 4-class model emits. File kept intact for future work.

## M3 — Accurate `model_mode` reporting
**Files:** `routes/visualization_routes.py`, `services/analysis_report.py`
Replaced `"fine_tuned"/"coco_fallback"` (which keyed off `FLOORPLAN_MODEL_PATH`, an env
var the Mask R-CNN loader never reads) with `"mask_rcnn_4class"`, and updated the two
docstring examples to match. The `analysis_report.model_mode` field is now truthful.

---

## C4 — IFC opening API bug (discovered BY the new validator) — FIXED
**File:** `export/ifc_exporter.py` (`_create_opening`, `_fill_opening`)
**Found how:** running the new post-export validator against a real ifcopenshell
**0.8.5** export. Doors/windows came out as orphans; stderr showed
`No module named 'ifcopenshell.api.void'`.
**Root cause:** `_create_opening` did `import ifcopenshell.api.void` at the top
(removed in 0.8.x) and called `feature.add_opening` (which no longer exists —
0.8.x renamed it to `feature.add_feature`). Both threw, so **every opening
failed and no door/window was cut into a wall** — the IFC silently lost all
openings.
**Fix:** removed the dead top-level `void` import; void/fill now try, in order,
`feature.add_feature` (0.8.x) → `feature.add_opening` (early 0.8) →
`void.add_opening` (0.7.x). Verified end-to-end: doors/windows now void and fill
their host walls correctly on ifcopenshell 0.8.5.

## NEW FEATURE — Model-standards validator (`validation/` package)
**New files:** `validation/__init__.py`, `validation/report.py`,
`validation/bim_checks.py`, `validation/ifc_checks.py`
**Wired into:** `routes/export_routes.py` (`POST /export/ifc`)
A two-stage "modeling standards" gate, policy = **block on critical, warn on minor**:
- **Pre-export** (`validate_bim_data`, pure Python) — geometric sanity
  (zero-length/zero-thickness, floating walls, rooms closed, openings on host
  wall), BIM completeness (heights/widths/host walls/unique ids), code-readiness
  (rooms typed + area, door widths — what the Mabhas checker needs).
- **Post-export** (`validate_ifc_file`, ifcopenshell) — valid IFC4 (parses,
  schema, one IfcProject, units), spatial tree, element placement (accepts the
  canonical door→opening→wall→storey fills chain), unique GlobalIds, openings
  void walls, recommended Psets.
Behaviour: a critical issue **blocks** the export (HTTP 400 + full JSON report)
and the bad file is discarded; warnings are attached to the file response as
`X-Model-Validation-*` headers. Call `POST /export/ifc` with `validate_only=true`
to get the pre-export report as JSON without building a file.
**Verified:** clean model → pre `pass`, post `warn` (only "missing recommended
Pset" warnings, accurate — the exporter writes `Pset_SpaceCommon` only). Broken
model → blocked with precise per-element criticals.
**Suggested next step (not done):** add `Pset_WallCommon` / `Pset_DoorCommon` /
`Pset_WindowCommon` in the exporter to clear the remaining warnings.

---

## Deferred (by your instruction)
- **H3 — wire in the YOLO supplementary detector.** Left untouched. `build_yolo_elements`
  remains uncalled. This is **Section 2** of the project and will be done after Section 1
  is complete.

## Not changed (Low priority, noted only)
- L1 mixed tabs/spaces, L2 `/analyze` not shown in Swagger (uses `@bp.route`),
  L3 generator variable `w` shadowing image width. None affect correctness.
EOF
echo "changelog created"
---

# IFC Interface Spec — Section 1 implementation

Implements the *Two-Step IFC Interface* brief: the exported IFC becomes the
single validated **contract** between Step 1 (photo → IFC) and Step 2
(IFC → verdicts). This round covers the Section-1 scope only — Workstream **A**
(exporter enrichment + export-time gate), **B1** (IFC→bim_data loader), **B2**
(confidence/review pre-pass), and the contract + round-trip tests.

**Deferred to Section 2 (engine code not in this repo):** Workstream **C** (IDS/
IfcTester lane, agent wiring, merge) and the §B3 *verdict-equality* round-trip,
which need `run_compliance`, the four agents, `SpatialGraph`, `ComplianceResult`
and `eval/test_verdict_regression.py` — all of which live in the separate
compliance-engine project. `ifctester` is likewise not installed here.

## Decisions taken (spec §9)
- **Length unit = MILLIMETRE**, plus an explicit **SQUARE_METRE** area unit so
  `Qto_SpaceBaseQuantities.NetFloorArea` reads in m². (§9.1)
- **Provenance Pset = `Pset_SimsysProvenance`**, contract Pset =
  `Pset_SimsysContract`. (§9.2)
- **Confidence threshold = 0.5**, env `REVIEW_CONFIDENCE_THRESHOLD`. (§9.3)
- **Interim confidence** (§9.4): detector confidence not yet threaded, so
  `Confidence=1.0`, `Source` per element type (maskrcnn / geometric / ocr),
  `NeedsReview` from the existing `name_source` / `needs_review` flags. The
  field stays in the contract so the model can populate it later.

## Workstream A — exporter (`export/ifc_exporter.py`)
- **Deterministic GlobalIds** (`_stable_guid`) on project/site/building/storey
  and every wall/door/window/space/slab/stair/opening — re-exporting the same
  `bim_data` yields identical ids (§A2).
- **`Pset_SimsysProvenance` on every element**, no null fields (`_add_provenance`):
  `OriginalId`, `Source`, `DetectorClass`, `Confidence`, `NeedsReview`,
  `ReviewReason`, plus `NameSource` for spaces (§A4).
- **`Pset_SimsysContract.ContractVersion = "1.0"`** on the project (§4).
- **Explicit m² area unit** declared alongside the mm length unit (§A1/§9.1).
- **`Qto_SpaceBaseQuantities`** (`NetFloorArea`, `GrossFloorArea` in m², `Height`
  in mm) on every space — a real computed area the IDS lane reads natively (§A3).
- **`Qto_WallBaseQuantities`** (`Width`=thickness, `Length`, `Height` in mm) and a
  **wall axis line** representation so the loader recovers wall geometry cleanly
  and Lane 2 can probe door sides (§A3).
- **Window `IsExternal` mirrored** from the host wall's `IsExternal` (§A5).
- Standard Psets completed for stairs (`Pset_StairCommon`) and slabs
  (`Pset_SlabCommon`); `Pset_WallCommon`/`DoorCommon`/`WindowCommon` retained.
- **§A7 export-time gate**: after writing, the exporter runs
  `validate_ifc_contract`; if the file violates the contract it is **deleted**
  and an `IfcContractError` is raised — a half-valid IFC is never emitted.

### ★ Geometry-scaling fix (×1000) — found by the new round-trip test
The high-level ifcopenshell geometry API (`edit_object_placement`,
`add_wall_representation`, `add_door_representation`, `add_window_representation`,
`add_axis_representation`, `add_profile_representation`) interprets its numeric
inputs as **SI metres** and rescales them to the file's unit on write. The
exporter was feeding **millimetres**, so every placement and extrusion came out
**×1000** — a 5000 mm wall sat at 5 000 000 (a 5 km building), and `IfcSpace`
footprint area disagreed with the stored `NetFloorArea` by 10⁶. Direct
attributes (`OverallWidth`/`OverallHeight`) and the manual footprint builder were
unaffected, which is why it stayed hidden until the round trip compared
coordinates.

**Fix:** every value handed to the high-level API is now multiplied by
`ifcopenshell.util.unit.calculate_unit_scale(model)` (0.001 for mm) via two
helpers (`_unit_scale`, `_scale_translation`); the manual builders
(`_make_extruded_polygon_rep`, `_rect_profile`) keep writing raw mm. **Verified:**
wall origins, door insertion points, sill heights, wall solids (2800 mm) and
opening boxes (600 mm) are now in true millimetres, and the round trip recovers
them exactly. This was a **pre-existing** exporter bug, not introduced by the
contract work.

## Workstream B — Step-2 entry loader (`interface/` — new package)
- **`interface/ifc_to_bim_data.py`** (B1): reconstructs the exact `bim_data`
  dict the agents consume, reading only the IFC (the inverse of §A3). Unit-aware
  (the single mm↔unit inverse lives here); ids come from
  `Pset_SimsysProvenance.OriginalId`; each element carries its provenance.
- **`interface/review_prepass.py`** (B2): `apply_review_prepass(bim_data,
  threshold)` annotates every element with a `review` block and flags those with
  `NeedsReview=true` or `Confidence < threshold`, plus a `_review_summary` for
  the report. Annotates the dict the Step-2 agents read; the agents are not
  modified.
- **`interface/__init__.py`**: exposes `ifc_to_bim_data`, `apply_review_prepass`.

## Validation additions (`validation/`)
- **`validate_ifc_contract`** (`validation/ifc_checks.py`): the §A7/§4 acceptance
  gate — schema, units, `ContractVersion`, GlobalId + non-null provenance on
  every physical element, every door/window voided+filled, every space with
  `Qto NetFloorArea` + footprint. Promotes the relevant issues to **critical**.
- **`IfcContractError`** (`validation/report.py`): carries the failed report.
- Exported from `validation/__init__.py`.
- `routes/export_routes.py`: catches `IfcContractError` from the exporter's gate
  and returns it as a structured validation error; the exporter gate is now the
  authoritative post-export check.

## Config
- `config/settings.py`: `REVIEW_CONFIDENCE_THRESHOLD` (env-overridable, default 0.5).

## Tests added (spec §8)
- `tests/test_ifc_contract.py` — asserts the full §4 checklist on a generated
  IFC (entities, deterministic ids, provenance non-null, standard Psets,
  OverallWidth/Height, Qto area + footprint, units incl. m², ContractVersion,
  and the mm-scale guard).
- `tests/test_ifc_roundtrip.py` — structural `bim → IFC → bim'` field-equality
  (walls/doors/windows/rooms, ids, host bindings, dims, areas, mm coordinates).
  Verdict-equality (§B3) is noted as Section 2.
- `tests/test_provenance_gate.py` — the §B2 pre-pass: `NeedsReview` / low
  confidence forces a flag; threshold configurable; end-to-end from an exported
  untyped-room plan.

**All 42 tests pass** (`test_ifc_contract` + `test_ifc_roundtrip` +
`test_provenance_gate` + existing `test_validation`) on ifcopenshell 0.8.5.
