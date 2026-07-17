# Floor Plan → IFC → Mabhas Compliance

Final unified release of a two-service pipeline:

```text
2D floor-plan image
        │
        ▼
Stage 1 — Floor-plan analysis API
Mask R-CNN + optional YOLO + OCR
        │
        ▼
Canonical BIM data + IFC Contract 1.2
        │
        ├── producer-side export validation
        └── independent Geometry/Provenance Gate
        │
        ▼
Stage 2 — Mabhas compliance engine
Schema → Quality → deterministic compliance
        │
        ▼
JSON + HTML + PDF + BCF reports
```

The repository contains both services and the versioned contracts between them.
The compliance engine never lets an LLM create or override deterministic
`PASS`/`FAIL` verdicts. Missing or untrusted information is reported as
`NEEDS_REVIEW` or `NOT_EVALUATED` instead of being invented.

## Final release status

- Stage 1 package/API: `2.8.0`
- Compliance engine package/API: `1.4.0`
- IFC handoff contract: `1.2`
- Target runtime: CPython `3.11`, Linux `amd64`, CUDA `11.8`
- Previous verified regression evidence:
  - Stage 1: 174 tests passed
  - Compliance engine: 572 tests passed
  - Phase 8 evaluator: 15 focused tests passed
- Empirical ML accuracy remains blocked until sealed weights and an adjudicated
  holdout dataset are supplied. Synthetic reference metrics are contract tests,
  not claims about model accuracy.
- Final cleanup verification on the review host:
  - 7 final-release cleanup tests passed;
  - 42 detector/packaging/evaluation tests passed;
  - 12 geometry/container tests passed;
  - 16/16 evaluation-infrastructure acceptance checks passed;
  - serialized contracts, Markdown links, dependency locks, container/security
    contracts, compileall, and deterministic SBOM generation passed.
- The review host did not contain Flask, IfcOpenShell, Ruff, Docker, CUDA, or the
  external model artifacts, so the complete 174/572 runtime suites were not
  re-executed during cleanup. Their last complete verified summaries are retained
  under `release/evidence/phase8/`; target-host acceptance remains mandatory.

Compact verification evidence is retained under `release/evidence/`. Historical
per-phase build outputs, duplicated reports, old checksum sets, and generated
JUnit archives were deliberately removed from the final source release.

## Repository layout

| Path | Purpose |
|---|---|
| `application.py`, `routes/` | Stage 1 Flask/OpenAPI service |
| `models/`, `analysis/`, `services/` | Detection, geometry, BIM semantics, orchestration |
| `export/`, `validation/` | IFC export and independent trust gates |
| `stage1_contracts/`, `contracts/` | Manual Inputs, Scale Evidence, provenance, IFC/OpenAPI contracts |
| `evaluation/` | Ground-truth ML evaluation and verdict-impact metrics |
| `compliance-engine/` | FastAPI/Celery compliance service and deterministic engine |
| `requirements/` | Hash-locked Stage 1 dependency sets |
| `sbom/` | CycloneDX software bills of materials |
| `scripts/` | Preflight, acceptance, evaluation, lock, SBOM, and release tools |
| `docs/` | Current architecture decisions and ML-evaluation protocol |
| `release/` | Compact final evidence and release metadata |

## External artifacts

The following large files are intentionally not bundled:

```text
wheels/torch-2.1.2+cu118-cp311-cp311-linux_x86_64.whl
wheels/torchvision-0.16.2+cu118-cp311-cp311-linux_x86_64.whl
weights/maskrcnn_15_epochs.h5
weights/yolo_best.pt
compliance-engine/models/huggingface/
compliance-engine/models/bge_reranker/
```

After copying them into place, seal their size and SHA-256 values:

```bash
python3.11 scripts/preflight.py \
  --mode full-pipeline \
  --refresh-manifest \
  --strict \
  --artifacts-only
```

## Production secrets

Create the secret files outside version control:

```bash
mkdir -p secrets
python -c "import secrets; print(secrets.token_urlsafe(48))" \
  > secrets/floorplan_api_keys.txt
python -c "import secrets; print(secrets.token_urlsafe(48))" \
  > secrets/compliance_api_key.txt
chmod 600 secrets/*.txt
```

Then copy `.env.example` to `.env` and set at least the allowed origins and
hosts for your deployment.

## Build and run

### Full pipeline with Docker Compose

```bash
cp .env.example .env
python3.11 scripts/preflight.py --mode full-pipeline --strict --artifacts-only
docker compose --profile full-pipeline build --no-cache
docker compose --profile full-pipeline up -d
```

The main public service is bound to loopback by default:

```text
http://127.0.0.1:8080
```

Place it behind a TLS reverse proxy for Internet-facing deployment. Redis and
the compliance API are kept on the internal Compose network.

### Stage 1 only

```bash
docker compose --profile floorplan-only build
docker compose --profile floorplan-only up -d
```

### Local Python development

Use CPython 3.11. The production build is defined by the hash-locked files in
`requirements/`; `requirements.txt` remains the human-readable top-level input.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements/baseline.lock
```

For the full ML runtime, install the runtime locks and provision the local CUDA
wheels as described in `requirements/README.md` and `wheels/README.md`.

## Public workflow

1. `POST /analyze` — image + Manual Inputs v1 + Scale Evidence v1
2. `POST /export/ifc` — export IFC Contract 1.2
3. `POST /compliance/jobs/from-analysis` or `/compliance/jobs/ifc`
4. `GET /compliance/jobs/{job_id}` or `/wait`
5. Download JSON, HTML, PDF, or BCF reports

Current OpenAPI snapshots:

```text
contracts/openapi_stage1.json
compliance-engine/docs/contracts/openapi.json
```

## Verification

Run the final acceptance entry point:

```bash
python3.11 scripts/run_final_acceptance.py --out release/local/final-acceptance
```

Individual checks:

```bash
make verify
make test
make acceptance
python3.11 scripts/generate_openapi.py --check
python3.11 -m compileall -q application.py routes services models evaluation export validation compliance-engine
```

Real ML holdout evaluation is documented in:

```text
docs/phase8/HOLDOUT_DATASET_PROTOCOL_FA.md
docs/phase8/REAL_EVALUATION_CHECKLIST_FA.md
```

## Trust model

The IFC boundary is intentionally defensive in depth:

1. The exporter resolves versioned Manual Inputs and Scale Evidence, builds
   correct geometry, validates it, and publishes atomically.
2. Stage 1 reopens the written file and independently checks geometry,
   relationships, counts, attributes, quantities, and provenance.
3. The compliance engine performs its own schema, geometry, and trace checks on
   every received IFC, including old, external, or edited files.

A file that merely contains plausible metadata but contradicts its Body or
relationships is rejected before compliance verdicts are evaluated.

## Final documentation

- `FINAL_CHANGELOG_FA.md` — complete project change history and removed files
- `FINAL_RUNBOOK_FA.md` — deployment, validation, backup, and maintenance steps
- `docs/ADR-009_FINAL_RELEASE_CLEANUP.md` — final cleanup and release decisions
- `compliance-engine/README.md` — engine-specific usage
- `compliance-engine/ARCHITECTURE.md` — engine architecture

## Known limitations

- Real image-model accuracy is not certified until a human-adjudicated holdout
  is run with the sealed runtime weights.
- External model directories and CUDA wheels must be supplied by the operator.
- Docker/GPU behavior must be validated on the target NVIDIA host.
- Process-local rate limiting should be complemented by shared gateway limits
  when multiple replicas are deployed.
- TLS termination, OIDC, or mTLS belong at the deployment gateway.
