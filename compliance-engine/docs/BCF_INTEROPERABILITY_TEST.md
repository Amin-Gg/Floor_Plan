# BCF Interoperability Verification Record

## Implementation under test

- Format: BCF XML 2.1
- Exporter: `reporting/bcf_exporter.py`
- Acceptance fixture: `tests/fixtures/sample_plan.ifc`
- Clause corpus: `data/mabhas_clauses.json`

## Automated verification

The automated acceptance run verifies:

- `bcf.version` is present and declares 2.1;
- `project.bcfp` is structurally valid when emitted;
- every topic directory has a UUID topic ID;
- every `markup.bcf` topic ID matches its directory;
- every referenced viewpoint exists;
- every exported topic has a viewpoint selecting a real 22-character IFC `GlobalId`;
- every referenced snapshot exists and is a readable PNG;
- topic and viewpoint IDs are stable;
- identical inputs and timestamp generate byte-identical BCF archives;
- topic count matches the manifest;
- malformed archives fail validation.

Acceptance fixture result:

```text
BCF version: 2.1
Topics: 14
Viewpoints: 14
IFC component selections: 14
Snapshots: 14
Global/unanchored findings skipped: 315
```

The skipped findings remain available in JSON, HTML, and PDF. Findings with
only an internal ID, including `QC-IDENT-001`, are explicitly skipped and
recorded in the manifest; the exporter never creates markup-only topics or
fabricates IFC component identifiers.

## External GUI viewer status

**Not executed in this container.** No independent desktop BCF viewer is
installed or available through the runtime. The package therefore does not
claim that a human opened the archive in a named GUI application.

Before a production interoperability claim is made, perform this manual check
in at least one independent BCF XML 2.1-capable viewer:

1. Import `compliance_issues.bcf` together with the source IFC.
2. Confirm all 14 topics import without corruption.
3. Open at least one Door, Window, Wall, and Space issue when present.
4. Confirm the selected element matches the topic's IFC `GlobalId`.
5. Confirm snapshot and camera framing are useful.
6. Record viewer name/version and any deviations below.

| Viewer | Version | Topics imported | Selection works | Snapshot works | Notes |
|---|---|---:|---|---|---|
| Not yet executed | — | — | — | — | Requires external desktop environment |

This limitation is environmental, not hidden by the automated acceptance
result.
