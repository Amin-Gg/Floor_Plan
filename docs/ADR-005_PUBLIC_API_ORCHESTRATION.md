# ADR-005 — Public API and orchestration boundary

**Status:** Accepted in Phase 5  
**Scope:** Floor-plan Stage 1 and the nested compliance engine

## Context

The two services have different dependency stacks and both contain top-level
packages with overlapping names. Importing engine internals inside Stage 1 made
test order, deployment topology and package resolution part of the contract.
The previous API also did not provide a stable job lifecycle, uniform error
shape, correlation IDs, or an explicit retry policy.

## Decision

### Supported boundaries

Production communication uses the public HTTP API only:

1. Stage 1 exports and validates IFC Contract 1.2.
2. Stage 1 submits the IFC once to `POST /analyze-ifc`.
3. Stage 1 polls `GET /jobs/{job_id}` with a bounded deadline.
4. Stage 1 proxies JSON, HTML, PDF and BCF report downloads.

Offline tests and tooling may use `python -m api.public_cli`. The CLI exchanges
one JSON document over stdin/stdout. Stage 1 does not import private engine
modules.

### Job lifecycle

The public states are `queued`, `running`, `completed` and `failed`. A 12-hex
job ID is minted by the engine. The status response may expose a stable error
code and safe message, but never a traceback. Local status writes are atomic;
Redis remains the production cross-process store.

### Correlation

Stage 1 accepts `X-Correlation-ID`, falling back to `X-Request-ID`, and only
mints a new ID if neither exists. The same ID is sent to the engine, stored on
the job and returned on both response headers. IDs are capped at 128 characters.

### Retry and timeout policy

- IFC submission is not retried because POST is not idempotent.
- Health, status and report GETs may retry 502/503/504 and transport failures.
- Connect/read timeouts and retry backoff are configurable.
- Job polling has an explicit overall deadline and interval.
- Report downloads have a configurable maximum byte count.

### Error contract

Both services return a machine-readable envelope:

```json
{
  "success": false,
  "request_id": "...",
  "error": {
    "code": "stable_code",
    "message": "safe description",
    "status": 422,
    "details": {}
  }
}
```

The engine temporarily retains a top-level `detail` alias for Final-R2 client
compatibility. New clients must use the `error` object.

### File lifecycle

- Stage 1 request-scoped IFC files are deleted in `finally` blocks.
- Engine incoming uploads are capped and deleted after the worker consumes them.
- Reports are retained according to the job-store TTL.
- Report filenames are derived by the engine and never used as arbitrary paths.

### OpenAPI

The committed snapshots are:

- `contracts/openapi_stage1.json`
- `compliance-engine/docs/contracts/openapi.json`

CI regenerates both and fails if code and snapshots differ.

## Consequences

The services can be deployed, versioned and tested independently. Network
failure is explicit rather than appearing as an import or package collision.
The trade-off is that orchestration now needs bounded polling and HTTP failure
handling. Production authentication, rate limiting and worker isolation remain
Phase 7 responsibilities.
