"""
application.py
==============
Main entry point for the FloorPlanTo3D Flask API.

Responsibilities
----------------
1. Create the OpenAPI (Flask) application and configure it.
2. Set up structured logging with per-request IDs.
3. Register blueprints (routes).
4. Register centralised error handlers.
5. Initialise the AI model inside the application context.

OpenAPI / Swagger UI
--------------------
Interactive API documentation is available at:
    http://localhost:8080/openapi/swagger     ← try every endpoint live
    http://localhost:8080/openapi/redoc       ← clean read-only reference
    http://localhost:8080/openapi/openapi.json ← raw JSON spec

These pages are generated automatically from the Pydantic schemas in schemas.py.
No manual documentation maintenance is needed.

Starting the server
-------------------
Development:
    python application.py

Production:
    APP_ENV=production gunicorn --config gunicorn.conf.py application:application
"""

import logging
import logging.config
import os
import sys
import uuid

from flask import g, request
from flask_cors import CORS
from flask_openapi3 import Info, OpenAPI, Tag

from config.settings import get_config

# ── Configuration ─────────────────────────────────────────────────────────────
app_config = get_config()

# ── Logging ───────────────────────────────────────────────────────────────────

class _RequestIdFilter(logging.Filter):
    """Inject per-request request_id into every LogRecord."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = getattr(g, "request_id", "-")
        except RuntimeError:
            record.request_id = "-"
        return True


def _configure_logging(cfg) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.LOG_LEVEL, logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(root.level)
    handler.addFilter(_RequestIdFilter())

    try:
        formatter = logging.Formatter(cfg.LOG_FORMAT)
        formatter.format(logging.LogRecord("test", logging.INFO, "", 0, "msg", [], None))
    except (KeyError, ValueError):
        formatter = logging.Formatter(cfg.LOG_FORMAT_FALLBACK)

    handler.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(handler)


_configure_logging(app_config)
logger = logging.getLogger(__name__)

# ── OpenAPI metadata ──────────────────────────────────────────────────────────

_info = Info(
    title="FloorPlanTo3D API",
    version="2.8.0",
    description=(
        "AI-powered floor plan analysis system. "
        "Accepts a photograph of a floor plan and returns structured BIM data "
        "(walls, doors, windows, rooms, stairs) in mm coordinates, "
        "plus a downloadable IFC4 file for Revit, ArchiCAD, and FreeCAD.\n\n"
        "**Workflow:**\n"
        "1. `POST /analyze` → upload image → receive `bim_data` JSON\n"
        "2. `POST /export/ifc` → resolve Manual Inputs v1 and export IFC Contract 1.2\n"
        "3. `POST /compliance/jobs/ifc` or `/compliance/jobs/from-analysis` "
        "→ submit to the public compliance-engine API\n"
        "4. Poll `/compliance/jobs/{job_id}` and download JSON/HTML/PDF/BCF reports\n\n"
        "**Authentication:** API key required in production via `Authorization: Bearer` or `X-API-Key`."
    ),
)

# Tags group endpoints in the Swagger UI sidebar
_tags = {
    "core":   Tag(name="Core",   description="Image analysis and BIM data generation"),
    "export": Tag(name="Export", description="IFC4 file generation for 3D modeling software"),
    "system": Tag(name="System", description="Health checks and server status"),
}

# ── Application factory ───────────────────────────────────────────────────────

def create_app(cfg=None) -> OpenAPI:
    """
    Create and configure the Flask/OpenAPI application.

    Parameters
    ----------
    cfg : Config, optional
        Override the environment-detected config. Useful in tests.
    """
    if cfg is None:
        cfg = app_config

    from utils.security import validate_stage1_production_security
    validate_stage1_production_security()

    # OpenAPI is a drop-in subclass of Flask — all Flask features work unchanged
    from utils.error_handlers import openapi_validation_error_response
    app = OpenAPI(
        __name__, info=_info,
        security_schemes={
            "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "API key"},
        },
        validation_error_status=422,
        validation_error_callback=openapi_validation_error_response,
    )
    app.debug = cfg.DEBUG

    app.config["MAX_CONTENT_LENGTH"] = cfg.MAX_UPLOAD_MB * 1024 * 1024

    # Validate and apply CORS — raises RuntimeError in production if not set
    cors_origins = cfg.CORS_ORIGINS
    if hasattr(cfg, "_get_cors"):
        cors_origins = cfg._get_cors()
    CORS(app, resources={r"/*": {"origins": cors_origins}})

    # ── Per-request ID ────────────────────────────────────────────────────────
    @app.before_request
    def _assign_request_id():
        incoming = (
            request.headers.get("X-Correlation-ID")
            or request.headers.get("X-Request-ID")
            or ""
        ).strip()
        g.request_id = incoming[:128] if incoming else uuid.uuid4().hex[:16]
        logger.debug("→ %s %s", request.method, request.path)

    @app.after_request
    def _log_response(response):
        logger.debug("← %s %s → %d", request.method, request.path, response.status_code)
        request_id = getattr(g, "request_id", "-")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Correlation-ID"] = request_id
        return response

    # Security is installed before route execution and is intentionally
    # independent of the heavyweight ML runtime.
    from utils.security import install_flask_security
    install_flask_security(app)

    # ── Blueprints ────────────────────────────────────────────────────────────
    # flask-openapi3 uses APIBlueprint instead of Blueprint for documented routes.
    # Routes that use the old Blueprint still work — they just won't appear in Swagger.
    from routes.accuracy_routes import bp as accuracy_bp
    from routes.export_routes import bp as export_bp
    from routes.health_routes import bp as health_bp
    from routes.visualization_routes import bp as visualization_bp
    from routes.compliance_routes import bp as compliance_bp

    app.register_api(health_bp)
    app.register_api(accuracy_bp)
    app.register_api(visualization_bp)
    app.register_api(export_bp)
    app.register_api(compliance_bp)

    # Freeze the public contract with global authentication requirements.
    # The two minimal orchestrator probes remain intentionally unauthenticated.
    spec = app.api_doc
    spec["security"] = [{"ApiKeyAuth": []}, {"BearerAuth": []}]
    for probe in ("/livez", "/readyz"):
        for operation in (spec.get("paths", {}).get(probe, {}) or {}).values():
            if isinstance(operation, dict):
                operation["security"] = []

    # ── Error handlers ────────────────────────────────────────────────────────
    from utils.error_handlers import register_error_handlers
    register_error_handlers(app)

    # ── AI model initialisation ───────────────────────────────────────────────
    skip_model_init = (
        os.getenv("FLOORPLAN_SKIP_MODEL_INIT", "0") == "1"
        or os.getenv("APP_ENV", "development").lower() == "testing"
    )
    with app.app_context():
        if skip_model_init:
            logger.info("Skipping AI model initialization for API/schema mode.")
            return app
        logger.info("Initialising AI inference runtime...")
        try:
            from utils.inference_executor import get_executor, isolation_mode
            if isolation_mode() == "process":
                get_executor().start()
                logger.info("Process-isolated inference worker initialised successfully.")
            else:
                from models.mask_rcnn_model import initialize_model
                initialize_model()
                logger.info("Primary Mask R-CNN model initialised successfully.")
                from models.yolo_detector import initialize_yolo, is_yolo_initialized
                initialize_yolo()
                if is_yolo_initialized():
                    logger.info("Supplementary YOLO detector initialised successfully.")
                else:
                    logger.info("Supplementary YOLO detector is disabled or unavailable.")
        except Exception as exc:
            logger.error("AI model initialisation failed: %s", exc, exc_info=True)
            _env = os.getenv("APP_ENV", "development").lower()
            if _env == "production":
                logger.critical(
                    "APP_ENV=production and model failed to load — refusing to start. "
                    "Set FLOORPLAN_MODEL_PATH to a valid checkpoint directory."
                )
                raise SystemExit(1) from exc
            logger.warning(
                "Server started without a loaded model (development mode). "
                "POST /analyze returns HTTP 503 until the model is available."
            )

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

application = create_app()

if __name__ == "__main__":
    api = app_config.get_api_config()
    logger.info("Starting FloorPlanTo3D API (development server)")
    logger.info("Swagger UI: http://%s:%s/openapi/swagger", api["HOST"], api["PORT"])
    logger.warning(
        "Development server active. "
        "For production: gunicorn --config gunicorn.conf.py application:application"
    )
    application.run(host=api["HOST"], port=api["PORT"], debug=api["DEBUG"])