# Final Review Fixes — Independent Implementation Audit

**Reviewed package:** `compliance-engine-final-reviewfixes.zip`  
**Reviewed change note:** `FINAL_REVIEW_FIXES.md`  
**Reference roadmap:** `STAGE8_REMEDIATION_ROADMAP(1).md`  
**Audit status:** Independent code inspection, diff against Phase 9, full test run, targeted bypass probes, reference verdict comparison, BCF probe, and acceptance execution.

---

## Final decision

**Status: PARTIALLY ACCEPTED — NOT READY TO TAG AS THE FINAL R2 RELEASE**

The changes are real, focused, and mostly correct. The package improves the Phase 9 codebase and independently passes all 546 tests. The documented reference verdicts and IFC-vs-bim_data equivalence also hold.

However, one high-impact input bypass remains in the low-level public compliance entry point, and the new identity-alert BCF behavior conflicts with the stricter Phase 8 roadmap policy. The ZIP is also explicitly a code-and-doc patch rather than a release package: it does not contain the regenerated Git tag, bundle, delivery manifest, or checksums.

Recommended merge decision:

```text
Do not discard the patch.
Do not publish it as stage8-phase9-final-r2 yet.
Apply the two code corrections below, add regression tests, then rebuild delivery artifacts.
```

---

# 1. Independent verification results

## 1.1 Full test suite

The environment initially lacked the declared `ifcopenshell` dependency. After installing the version range declared by the project:

```text
ifcopenshell 0.8.5
```

the suite was executed with unraisable warnings promoted to errors:

```bash
pytest -q -W error::pytest.PytestUnraisableExceptionWarning
```

Independent result:

```text
546 tests
546 passed
0 failed
0 errors
0 skipped
0 PytestUnraisableExceptionWarning
```

The supplied JUnit file also reports:

```text
546 tests
0 failures
0 errors
0 skipped
```

## 1.2 Reference verdict parity

The reference IFC and complete clause corpus were run through the unified pipeline.

Result:

```text
PASS              15
FAIL               9
NEEDS_REVIEW     309
NOT_EVALUATED      8
TOTAL             341
```

The same generated `bim_data` seam was then re-run through the `bim_data` pipeline entry point.

Result:

```text
IFC findings:      341
bim_data findings: 341
Compliance signatures equal: true
```

## 1.3 Acceptance execution

Executed:

```bash
python -m scripts.run_validation_acceptance \
  --ifc tests/fixtures/sample_plan.ifc \
  --manual-inputs tests/fixtures/remediation_manual_inputs.json \
  --output-dir <audit-output>
```

Result:

```text
ok: true
schema_status: passed
quality_status: failed intentionally by injected acceptance defects
JSON: generated
HTML: generated
PDF: generated
BCF: generated
BCF manifest: generated
```

Per-window overrides were applied independently:

```text
Wb.width_mm = 1400
W-ACCEPT-02.width_mm = 900
```

---

# 2. Diff scope

The review-fixes ZIP differs from the Phase 9 package only in the intended areas:

```text
api/main.py
services/validation_pipeline.py
manual_inputs/__init__.py
manual_inputs/legacy_guard.py                  NEW
validation/quality/checks/identity_integrity.py NEW
validation/quality/checks/__init__.py
validation/quality/registry.py
scripts/run_validation_acceptance.py
docs/adr/ADR-002-unified-finding-contract.md
docs/FINAL_REVIEW_FIXES.md                      NEW
docs/test-results/final_review_fixes_full.xml   NEW
tests/pipeline/test_legacy_building_params_guard.py NEW
tests/validation/quality/test_identity_integrity.py NEW
two registry-order tests
```

No unrelated runtime modules were silently replaced.

---

# 3. Fix-by-fix assessment

## Fix 1 — Removed flat `building_params`

### Result

**PARTIAL — API and unified pipeline are fixed, but the low-level public entry point remains bypassable.**

### Correctly implemented

The following paths now reject flat `building_params`:

```text
POST /analyze
PipelineRequest(source_type="bim_data")
Celery/local job execution through run_validation_pipeline
direct calls to run_validation_pipeline
```

The error is explicit and includes offending keys.

The exact Phase 9 H1 bypass now raises `ManualInputsError` when called through the unified pipeline.

Marker-only inputs remain harmless:

```json
{}
{"_provided": []}
{"_provided": ["wall_height"]}
```

They do not provide values and do not create a PASS.

### Remaining high-impact bypass

`services/orchestrator.py` still documents `run_compliance()` as its public entry point and still accepts both:

```python
run_compliance(
    bim_data_with_building_params,
    clauses
)
```

and:

```python
run_compliance(
    bim_data,
    clauses,
    building_params={"ceiling_height_mm": 2900}
)
```

This function does not invoke `reject_legacy_building_params()`.

Independent probe using the canonical IFC-derived seam:

```text
No parameter:
H1 = NOT_EVALUATED

Embedded flat building_params:
H1 = PASS, measured 2.9 m

building_params function argument:
H1 = PASS, measured 2.9 m
```

Therefore the statement that “every channel, including direct calls, now fails loudly” is not correct.

### Why this matters

This is still a verdict-driving route that bypasses:

- Manual Inputs v1 type checks;
- range checks;
- cross-field checks;
- source/provenance resolution;
- unified stage ordering;
- Quality validation.

It also conflicts with the roadmap rule that all public entry points must delegate to one pipeline contract.

### Required correction

Preferred architecture:

1. Rename the low-level function to an explicitly internal name such as:

   ```python
   _run_compliance_core(...)
   ```

2. Remove the public `building_params` argument.
3. Remove documentation presenting it as a supported public entry point.
4. Make all external tests and callers use:

   ```python
   run_validation_pipeline(PipelineRequest(...))
   ```

5. Keep only the unified pipeline responsible for validated manual-input merging.

Alternative, less clean:

- add a trusted internal execution context and reject flat input in every untrusted call.

Do not add a simple guard at the start of `run_compliance()` without redesigning the internal seam, because the canonical pipeline currently sends validated resolved values inside the internal `bim_data["building_params"]` block.

### Required regression tests

```text
test_direct_run_compliance_is_not_public_or_rejects_untrusted_params
test_services_orchestrator_has_no_public_building_params_argument
test_all_documented_entrypoints_delegate_to_run_validation_pipeline
```

---

## Fix 1b — Claimed canonical-seam re-ingestion

### Result

**DOCUMENTATION/CONTRACT MISMATCH**

The change note says the internal round-trip seam remains re-ingestable.

That is true only when `building_params` is empty or marker-only.

It is false for a seam containing validated Manual Inputs values.

Independent probe:

```text
Pipeline with Manual Inputs v1 emits:

{
  "ceiling_height_mm": 2900.0,
  "_provided": ["ceiling_height_mm"]
}

Re-ingesting that exact bim_data output:

ManualInputsError:
building_params was removed in Phase 9 ...
offending key: ceiling_height_mm
```

### Required decision

Choose and document one model:

#### Option A — Public bim_data is never a round-trip format

This is the cleaner security boundary.

Document:

```text
The enriched bim_data output is an internal agent seam and is not accepted as
a public input. Use BuildingModel for internal typed reuse, or provide the
original bim_data plus Manual Inputs v1 again.
```

Then remove the claim that the exported canonical seam is re-ingestable.

#### Option B — Provide a trusted serialized canonical model format

Create a separate versioned internal format with explicit trust/provenance and a distinct ingestion endpoint. Do not reuse public raw `bim_data`.

---

## Fix 2 — Geometry-fallback identity alert

### Result

**QUALITY PLUGIN: COMPLETE**

The implementation is real and well placed:

```text
QC-IDENT-001
plugin name: identity_integrity
registered immediately after contract_read
blocking: false
```

It:

- scans all expected element collections;
- emits one finding per fallback-identified element;
- includes internal identity;
- does not invent IFC GUIDs;
- correctly treats addressability degradation as non-blocking;
- is covered by focused and registry tests.

The plugin is inert on the reference IFC, so verdict parity remains unchanged.

### BCF handling

**PARTIAL / POLICY MISMATCH**

The test correctly proves that no fake IFC GUID is produced.

However, the exporter currently creates markup-only BCF topics for internal-ID-only findings:

```text
reason: markup-only internal element reference
viewpoint: none
component selection: none
```

An independent fallback-identity probe produced:

```text
topics_total: 5
viewpoints_total: 0
component_selection_topics: 0
legacy_reference_topics: 5
```

The Phase 8 roadmap and project history state that only findings with a trustworthy model anchor should become BCF topics, and that unanchored findings should remain in JSON/HTML/PDF.

An internal UUID is not selectable in an IFC-based BCF viewer.

### Required correction

For `QC-IDENT-001` and other findings with no valid IFC GUID:

```text
skip BCF topic creation
record explicit skip reason in manifest
retain finding in JSON/HTML/PDF
```

Recommended skip reason:

```text
element has no trustworthy IFC GlobalId; BCF component selection unavailable
```

Update the test to require a skip rather than allowing either a markup-only topic or a skip.

---

## Fix 3 — ADR ordinal amendment

### Result

**COMPLETE**

The ADR now matches implementation:

```text
v1
US stage
US code
US model_fingerprint
US element_key
US clause_id
[US ordinal, only when ordinal > 0]
```

Ordinal zero preserves previous stable IDs.

The implementation already appends the ordinal only when non-zero.

No runtime behavior change was introduced.

---

## Fix 4 — Complete acceptance artifact set

### Result

**MOSTLY COMPLETE**

The acceptance command now raises a descriptive error when any required report kind returns no path:

```text
json
html
pdf
bcf
```

The error explains the WeasyPrint and pango/cairo requirement.

The real acceptance run produced all required files successfully.

### Minor hardening recommendation

The check currently verifies that report-map values are truthy, not that every target exists and is non-empty.

Stronger release-gate validation:

```python
for kind in required:
    path = reports.get(kind)
    if not path or not Path(path).is_file() or Path(path).stat().st_size == 0:
        missing.append(kind)
```

BCF existence is indirectly validated later, but JSON/HTML/PDF should also be explicitly checked.

This is low priority.

---

## Fix 5 — Acceptance command references

### Result

**COMPLETE IN THE UPLOADED ROADMAP**

The corrected roadmap now uses:

```bash
--ifc tests/fixtures/sample_plan.ifc
```

and accurately describes the injected-defect acceptance scenario.

However, the corrected roadmap file is not included inside the code ZIP. It was supplied separately.

Include it in the final tagged repository if it is considered part of release documentation.

---

# 4. Delivery-package status

## Result

**NOT A FINAL RELEASE PACKAGE**

The change note explicitly says this ZIP contains code and documentation only and requires local regeneration of release evidence.

The ZIP does not contain:

```text
updated Git repository metadata
stage8-phase9-final-r2 tag evidence
transferable Git bundle
updated SHA256SUMS
updated GIT_EVIDENCE
final delivery manifest
rebuilt final wheel/release artifact evidence
```

This is acceptable as a patch package, but it must not replace the Phase 9 final delivery ZIP as-is.

After code corrections, rebuild:

```text
_delivery/
Git bundle
GIT_EVIDENCE
SHA256SUMS
JUnit evidence
acceptance outputs
wheel
file manifest
```

---

# 5. Known limitations confirmed

The following remain open and are correctly identified as non-blocking product or external-verification items:

```text
Window(Door) inheritance cleanup
desktop BCF GUI viewer import verification
309 NEEDS_REVIEW findings
```

The BCF GUI viewer item is still an unmet roadmap exit condition for declaring external interoperability fully verified.

---

# 6. Final scorecard

| Area | Status |
|---|---|
| Diff scope and code hygiene | PASS |
| 546-test evidence | PASS |
| IfcOpenShell warning gate | PASS |
| Reference verdict parity | PASS |
| IFC vs bim_data equivalence | PASS for the tested unmodified seam |
| API `/analyze` legacy rejection | PASS |
| Unified pipeline legacy rejection | PASS |
| Direct `run_compliance` rejection | FAIL |
| Enriched canonical seam re-ingestion claim | FAIL / contract mismatch |
| QC-IDENT-001 plugin | PASS |
| No fabricated IFC GUID | PASS |
| Strict BCF trustworthy-anchor policy | PARTIAL |
| ADR ordinal documentation | PASS |
| Acceptance complete artifact behavior | PASS with minor hardening |
| Correct acceptance command | PASS |
| Final Git/tag/checksum delivery | NOT DELIVERED |

---

# 7. Required work before R2 release

## Blocking

### R2-001 — Close the direct orchestrator bypass

Remove or internalize the public `run_compliance(..., building_params=...)` path and migrate all supported callers to the unified pipeline.

### R2-002 — Resolve seam contract

Either:

- document that enriched `bim_data` is output-only; or
- create a distinct trusted serialized canonical-model format.

Do not claim full seam re-ingestion while value-bearing blocks are rejected.

### R2-003 — Enforce strict BCF anchor policy

Skip findings without a real IFC GUID rather than producing markup-only internal-ID topics.

## Then rebuild delivery

```text
commit
tag stage8-phase9-final-r2
full pytest
acceptance run
wheel
Git bundle
checksums
manifest
delivery ZIP
```

---

# 8. Recommended release decision

```text
Current code quality: good
Test quality: strong
Regression safety: strong
Architecture closure: not yet complete
Release readiness: hold
```

The patch should be merged after the three R2 blocking corrections, not rejected wholesale.
