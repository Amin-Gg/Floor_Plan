"""
application.py
==============
Main entry point for the FloorPlanTo3D Flask API.

Responsibilities
----------------
1. Create the Flask application and configure it.
2. Set up structured logging with per-request IDs.
3. Register blueprints (routes).
4. Register centralized error handlers.
5. Initialize the AI model inside the application context.

What this file does NOT do
---------------------------
It does not import analysis helpers, image processing utilities, or
visualization modules — those belong in the route files that use them.
Adding imports here is the single most common cause of confusing startup
errors and unnecessarily slow cold starts.

Starting the server
-------------------
Development:
    python application.py

Production (recommended):
    APP_ENV=production gunicorn --config gunicorn.conf.py application:application
"""

import os
import sys
import uuid
import logging
import logging.config

from flask import Flask, g, request
from flask_cors import CORS

from config.settings import get_config

# ── Configuration ─────────────────────────────────────────────────────────────
app_config = get_config()

# ── Logging ───────────────────────────────────────────────────────────────────
# Configure before any other import so the format applies everywhere.
# We use a filter to inject the per-request request_id into every log record,
# which makes it possible to trace one request through interleaved log lines.

class _RequestIdFilter(logging.Filter):
    """Add request_id to every LogRecord so the format string can use it."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = getattr(g, "request_id", "-")
        except RuntimeError:
            # Outside of a request context (startup, shutdown) g is not available
            record.request_id = "-"
        return True


def _configure_logging(cfg) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.LOG_LEVEL, logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(root.level)
    handler.addFilter(_RequestIdFilter())

    try:
        # Use the format with %(request_id)s if the filter is in place
        formatter = logging.Formatter(cfg.LOG_FORMAT)
        # Test the format works (raises KeyError if filter not applied yet)
        formatter.format(logging.LogRecord(
            "test", logging.INFO, "", 0, "msg", [], None
        ))
    except KeyError:
        formatter = logging.Formatter(cfg.LOG_FORMAT_FALLBACK)

    handler.setFormatter(formatter)

    # Remove any handlers Flask or other imports may have added already
    root.handlers.clear()
    root.addHandler(handler)


_configure_logging(app_config)
logger = logging.getLogger(__name__)

# ── Application factory ───────────────────────────────────────────────────────

def create_app(cfg=None) -> Flask:
    """
    Create and configure the Flask application.

    Parameters
    ----------
    cfg : Config, optional
        Pass a Config instance to override the environment-detected config.
        Useful in tests:  app = create_app(TestingConfig())
    """
    if cfg is None:
        cfg = app_config

    app = Flask(__name__)
    app.debug = cfg.DEBUG

    # Maximum upload size — enforced by Werkzeug before our route code runs
    app.config["MAX_CONTENT_LENGTH"] = cfg.MAX_UPLOAD_MB * 1024 * 1024

    # CORS
    CORS(app, resources={r"/*": {"origins": cfg.CORS_ORIGINS}})

    # ── Per-request ID ────────────────────────────────────────────────────────
    # Assigned before every request so log lines can be grouped by request.
    @app.before_request
    def _assign_request_id():
        g.request_id = str(uuid.uuid4())[:8]
        logger.debug(
            "→ %s %s  [content-type: %s]",
            request.method, request.path, request.content_type
        )

    @app.after_request
    def _log_response(response):
        logger.debug("← %s %s → %d", request.method, request.path, response.status_code)
        # Attach request ID to the response header so clients can correlate
        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        return response

    # ── Blueprints ────────────────────────────────────────────────────────────
    from routes.health_routes        import bp as health_bp
    from routes.accuracy_routes      import bp as accuracy_bp
    from routes.visualization_routes import bp as visualization_bp
    from routes.export_routes        import bp as export_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(accuracy_bp)
    app.register_blueprint(visualization_bp)
    app.register_blueprint(export_bp)

    # ── Error handlers ────────────────────────────────────────────────────────
    from utils.error_handlers import register_error_handlers
    register_error_handlers(app)

    # ── AI model initialization ───────────────────────────────────────────────
    # Run inside the app context so any extension that needs it has access.
    # A failed init is logged but does not crash the server — the /health
    # endpoint will report the model as unavailable, and individual routes
    # raise ModelNotReadyError (→ HTTP 503) if called without a loaded model.
    with app.app_context():
        logger.info("Initializing AI model (Mask2Former Swin-Large)...")
        try:
            from models.mask_rcnn_model import initialize_model
            initialize_model()
            logger.info("AI model initialized successfully.")
        except Exception as exc:
            logger.error("AI model initialization failed: %s", exc, exc_info=True)
            logger.warning(
                "Server will start without a loaded model. "
                "POST /analyze will return HTTP 503 until the model is available."
            )

    return app


# ── Entry point ───────────────────────────────────────────────────────────────

# `application` is the name Gunicorn expects: `application:application`
application = create_app()

if __name__ == "__main__":
    api = app_config.get_api_config()
    logger.info("Starting FloorPlanTo3D API (development server)")
    logger.info("Host: %s  Port: %s  Debug: %s", api["HOST"], api["PORT"], api["DEBUG"])
    logger.warning(
        "You are using Flask's built-in development server. "
        "For production use: gunicorn --config gunicorn.conf.py application:application"
    )
    application.run(host=api["HOST"], port=api["PORT"], debug=api["DEBUG"])
