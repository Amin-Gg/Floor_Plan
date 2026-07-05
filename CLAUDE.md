# CLAUDE.md

## Project Overview

This repository implements an AI-powered floor plan analysis system with two main application surfaces:

1. `application.py` - the root Flask/OpenAPI service for image-based floor plan parsing, BIM JSON output, and IFC export.
2. `compliance-engine/` - a separate compliance pipeline built around FastAPI, Celery job orchestration, and regulation retrieval.

The codebase is organized into functional packages for model inference, image processing, validation, export, and compliance reasoning.

---

## Main Modules

### Root API and application layer
- `application.py` - primary entrypoint for the FloorPlanTo3D Flask/OpenAPI app.
- `gunicorn.conf.py` - production worker configuration for Gunicorn.
- `routes/` - route definitions for health, analysis, accuracy, visualization, and IFC export.
- `schemas.py` - shared schema definitions used by the Flask/OpenAPI layer.
- `utils/` - shared helpers for geometry, file handling, inference execution, validation, and error handling.

### Model and detection
- `models/mask_rcnn_model.py` - entrypoint to initialize the Mask R-CNN model used for floor-plan object detection.
- `models/yolo_detector.py` - alternate object detection support using YOLO.
- `mrcnn/` - Mask R-CNN training/inference support modules and utilities.
- `image_processing/` - image loading and mask processing helpers.

### BIM / export / validation
- `services/` - core services for JSON conversion, BIM building, validation, accuracy calculation, and image validation.
- `export/ifc_exporter.py` - converts generated BIM JSON into IFC4 output.
- `validation/` - IFC/BIM validation rules and report assembly.
- IFC-to-BIM conversion and review pre-pass logic live in `compliance-engine/ingest/` (single source of truth; Section-1 round-trip tests load it via `tests/_engine_modules.py`).

### Compliance engine
- `compliance-engine/api/main.py` - FastAPI entrypoint for a compliance job queue.
- `compliance-engine/api/tasks.py` - Celery task submission and job status handling.
- `compliance-engine/ingest/` - IFC ingestion, BIM extraction, and review preprocessing.
- `compliance-engine/rag/` - retrieval-augmented generation, graph indexing, and query routing.
- `compliance-engine/services/` - compliance-specific reasoning services and report generation.
- `compliance-engine/eval/` - evaluation, metrics, and retrieval testing.
- `compliance-engine/classification/` - classification helpers for clause matching.

---

## Entry Points

### Primary application
- `application.py` defines `create_app()` and `application = create_app()`.
- Runs directly via `python application.py` for development.
- In production, `gunicorn --config gunicorn.conf.py application:application` is expected.

### Compliance service
- `compliance-engine/api/main.py` exposes FastAPI routes:
  - `POST /analyze` to submit compliance jobs.
  - `GET /jobs/{job_id}` to poll status.
  - `GET /jobs/{job_id}/report/{kind}` to download completed reports.
  - `GET /health` to check service liveness.

### Scripts and helpers
- `compliance-engine/ingest/run_ifc.py` - runnable pipeline for IFC ingestion.
- `compliance-engine/rag/build_regulation_graph.py` - builds the regulation graph used by retrieval.
- `compliance-engine/eval/*.py` - CLI-style evaluation scripts and entrypoints.

---

## Data Flow

### Floor plan analysis flow
1. Client uploads an image to `POST /analyze` in the Flask API.
2. `routes/visualization_routes.py` orchestrates image preprocessing, model inference, segmentation, and BIM extraction.
3. `models.mask_rcnn_model.py` / `mrcnn/` perform detection and instance mask processing.
4. `services/`, `utils/`, and `validation/` convert detection output into structured BIM JSON.
5. `export/ifc_exporter.py` can convert BIM JSON into an IFC4 file when `POST /export/ifc` is called.

### Compliance job flow
1. Client submits BIM JSON to `compliance-engine/api/main.py`.
2. `compliance-engine/api/tasks.py` enqueues the request and tracks job status.
3. Worker tasks route through `compliance-engine/services/` and `compliance-engine/rag/` to build compliance reports.
4. `compliance-engine/validation` and `compliance-engine/services/report_generator.py` assemble results.
5. Completed jobs can be downloaded via report endpoints.

---

## Important Classes and Functions

### Root API
- `application.create_app()` - configures Flask/OpenAPI, CORS, request logging, error handlers, and model initialization.
- `_RequestIdFilter` - enriches all logs with per-request IDs.
- `routes/visualization_routes.analyze_floor_plan()` - main analysis handler for the root service.
- `routes/export_routes.export_ifc()` - IFC export endpoint.
- `routes/health_routes.health_check()` - system health endpoint.

### Model and inference
- `models.mask_rcnn_model.initialize_model()` - loads the Mask R-CNN model into memory.
- `mrcnn.model.MaskRCNN.compile()` - model compilation hotspot.
- `utils.inference_executor.InferenceExecutor` - orchestrates batched model inference.
- `image_processing.image_loader` and `image_processing.mask_processing` - image preparation.

### Validation and reporting
- `validation.report.ValidationReport` methods: `info()`, `add()`, `warn()`.
- `validation.bim_checks.validate_bim_data()` - checks BIM output correctness.
- `validation.ifc_checks.validate_ifc_file()` - IFC validation logic.
- `export.ifc_exporter.bim_json_to_ifc()` - IFC generation.

### Compliance engine
- `compliance-engine.api.tasks.submit_job()` - entry for enqueuing compliance jobs.
- `compliance-engine.rag.rag_retriever.MabhasRetriever.hybrid_retrieve()` - retrieval pipeline hotspot.
- `compliance-engine.rag.crag_retriever.CorrectiveRetriever.retrieve()` - retrieval core.
- `compliance-engine.services.report_generator.generate_reports()` - report assembly.
- `compliance-engine.services.spatial_graph.SpatialGraph.get_rooms_by_category()` - spatial graph reasoning.

---

## Dependencies

The repository has two primary dependency sets:

- Root service dependencies in `requirements.txt`:
  - `Flask`, `Flask-Cors`, `flask-openapi3`, `gunicorn`, `Pillow`, `PyYAML`, plus model/vision libraries.
- Compliance engine dependencies in `compliance-engine/requirements.txt`:
  - `fastapi`, `pydantic`, `celery`, `uvicorn`, `ifcopenshell`, `anthropic`, `huggingface-hub`, `groq`, plus graph/retrieval tooling.

Core runtime libraries include:
- `torch` / `mrcnn` for model inference
- `ifcopenshell` for IFC generation and validation
- `pydantic` / OpenAPI for request validation
- Celery / Redis for asynchronous compliance jobs

---

## Hotspots and High-Risk Areas

Key hotspots identified in the codebase:
- `validation.report.ValidationReport.info` / `add` / `warn` - centralized report assembly.
- `mrcnn.model.MaskRCNN.compile` - model setup path.
- `mrcnn.utils.resize` - image resizing and preprocessing.
- `compliance-engine.rag.crag_retriever.CorrectiveRetriever.retrieve` - compliance retrieval core.
- `compliance-engine.rag.rag_retriever.MabhasRetriever.hybrid_retrieve` - hybrid retrieval logic.
- `compliance-engine.services.spatial_graph.SpatialGraph.get_rooms_by_category` - spatial reasoning helper.

Also treat `application.py` and `compliance-engine/api/main.py` as high-value maintenance files because they define the public API surface.

---

## Editing Guidelines for Claude Code

1. Preserve the separation between the root floor-plan API and the `compliance-engine` service.
2. Update `routes/` when changing public endpoints; keep business logic in `services/`, `validation/`, or `models/`.
3. Do not alter `application.py` logging or request lifecycle handling unless necessary for cross-cutting concerns.
4. Keep model initialization isolated in `models/mask_rcnn_model.py`; avoid duplicating `initialize_model()` semantics elsewhere.
5. When modifying data contracts, update both `schemas.py` and the associated route request/response handlers.
6. For IFC export changes, validate with both `export/ifc_exporter.py` and `validation/ifc_checks.py`.
7. Use existing tests under `tests/` and `compliance-engine/tests/` for regression coverage.
8. Keep compliance job flow asynchronous; do not collapse `FastAPI` job endpoints with the root Flask service.

---

## Recommended Next Steps

- Review `routes/visualization_routes.py` for the main floor-plan analysis pipeline.
- Review `compliance-engine/api/main.py` if you need to extend compliance job APIs.
- Use `utils/error_handlers.py` for consistent error responses.
- Keep production and development startup instructions in sync with `application.py` and `gunicorn.conf.py`.
