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

import os
import sys
import uuid
import logging
import logging.config

from flask import g, request
from flask_cors import CORS
from flask_openapi3 import OpenAPI, Info, Tag

from config.settings import get_config
from utils.inference_executor import InferenceExecutor

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
    version="2.0.0",
    description=(
        "AI-powered floor plan analysis system. "
        "Accepts a photograph of a floor plan and returns structured BIM data "
        "(walls, doors, windows, rooms, stairs) in mm coordinates, "
        "plus a downloadable IFC4 file for Revit, ArchiCAD, and FreeCAD.\n\n"
        "**Workflow:**\n"
        "1. `POST /analyze` → upload image → receive `bim_data` JSON\n"
        "2. `POST /export/ifc` → convert `bim_data` to IFC4 file\n"
        "3. Open IFC in Revit / run Dynamo scripts for code compliance\n\n"
        "**Authentication:** None (add via reverse proxy for production)"
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

    # OpenAPI is a drop-in subclass of Flask — all Flask features work unchanged
    app = OpenAPI(__name__, info=_info)
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
        g.request_id = str(uuid.uuid4())[:8]
        logger.debug("→ %s %s", request.method, request.path)

    @app.after_request
    def _log_response(response):
        logger.debug("← %s %s → %d", request.method, request.path, response.status_code)
        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        return response

    # ── Blueprints ────────────────────────────────────────────────────────────
    # flask-openapi3 uses APIBlueprint instead of Blueprint for documented routes.
    # Routes that use the old Blueprint still work — they just won't appear in Swagger.
    from routes.health_routes        import bp as health_bp
    from routes.accuracy_routes      import bp as accuracy_bp
    from routes.visualization_routes import bp as visualization_bp
    from routes.export_routes        import bp as export_bp

    app.register_api(health_bp)
    app.register_api(accuracy_bp)
    app.register_api(visualization_bp)
    app.register_api(export_bp)

    # ── Error handlers ────────────────────────────────────────────────────────
    from utils.error_handlers import register_error_handlers
    register_error_handlers(app)

    # ── AI model initialisation ───────────────────────────────────────────────
    with app.app_context():
        logger.info("Initialising AI model (Mask2Former Swin-Large)...")
        try:
            from models.mask_rcnn_model import initialize_model
            initialize_model()
            logger.info("AI model initialised successfully.")
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
