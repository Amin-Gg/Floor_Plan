"""
utils/error_handlers.py
=======================
Centralised error handling for the FloorPlanTo3D Flask API.

Usage
-----
In application.py:

    from utils.error_handlers import register_error_handlers
    register_error_handlers(application)

In any route:

    from utils.error_handlers import (
        APIError, ValidationError, ImageValidationError, ModelNotReadyError
    )

    raise ValidationError("scale_factor must be positive")
    raise ModelNotReadyError()

Every error — whether raised explicitly or as an unhandled exception —
returns the same JSON envelope so clients only need one error-handling path:

    {
        "success":    false,
        "request_id": "a3f1b2c4",
        "error": {
            "message": "Missing required field 'image'",
            "code":    400,
            "type":    "ValidationError",
            "details": { ... }     ← optional, only present when useful
        }
    }
"""

import logging
import traceback
import uuid

from flask import jsonify, request, g

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class APIError(Exception):
    """
    Base class for all API errors.
    Raise subclasses in route handlers — the registered error handler converts
    them to a consistent JSON response automatically.
    """
    status_code: int = 500
    error_type:  str = "APIError"

    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        body = {
            "message": self.message,
            "code":    self.status_code,
            "type":    self.error_type,
        }
        if self.details:
            body["details"] = self.details
        return body


class ValidationError(APIError):
    """
    Raised when a request parameter fails validation.
    Results in HTTP 400 Bad Request.

    Example:
        raise ValidationError(
            "scale_factor_mm_per_pixel must be between 0.01 and 100",
            details={"received": 0, "min": 0.01, "max": 100}
        )
    """
    status_code = 400
    error_type  = "ValidationError"


class ImageValidationError(ValidationError):
    """
    Raised when the uploaded image is missing, empty, or unreadable.
    Results in HTTP 400 Bad Request.
    """
    error_type = "ImageValidationError"


class ModelNotReadyError(APIError):
    """
    Raised when a route requires the AI model but it is not yet initialized.
    Results in HTTP 503 Service Unavailable.
    """
    status_code = 503
    error_type  = "ModelNotReadyError"

    def __init__(self):
        super().__init__(
            "The AI model is not yet initialized. "
            "Check /health for status and server logs for details."
        )


class NotFoundError(APIError):
    """HTTP 404."""
    status_code = 404
    error_type  = "NotFoundError"


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _request_id() -> str:
    """Return the request-scoped ID if available, otherwise a fallback."""
    return getattr(g, "request_id", "n/a")


def _build_response(error_dict: dict, status_code: int):
    """Wrap an error dict in the standard envelope and return a Flask response."""
    body = {
        "success":    False,
        "request_id": _request_id(),
        "error":      error_dict,
    }
    return jsonify(body), status_code


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

def register_error_handlers(app) -> None:
    """
    Attach all error handlers to the Flask application.
    Call this once in application.py after creating the Flask app.

        from utils.error_handlers import register_error_handlers
        register_error_handlers(application)
    """

    # ── Our custom exceptions ─────────────────────────────────────────────────
    @app.errorhandler(APIError)
    def handle_api_error(exc: APIError):
        if exc.status_code >= 500:
            logger.error(
                "[%s] %s: %s",
                _request_id(), exc.error_type, exc.message,
                exc_info=True
            )
        else:
            logger.warning(
                "[%s] %s: %s",
                _request_id(), exc.error_type, exc.message
            )
        return _build_response(exc.to_dict(), exc.status_code)

    # ── Standard HTTP errors ──────────────────────────────────────────────────
    @app.errorhandler(400)
    def handle_bad_request(exc):
        return _build_response({
            "message": "Bad request — check your request format and parameters.",
            "code":    400,
            "type":    "BadRequest",
        }, 400)

    @app.errorhandler(404)
    def handle_not_found(exc):
        return _build_response({
            "message": f"Endpoint not found: {request.method} {request.path}",
            "code":    404,
            "type":    "NotFound",
        }, 404)

    @app.errorhandler(405)
    def handle_method_not_allowed(exc):
        return _build_response({
            "message": f"Method {request.method} is not allowed on {request.path}.",
            "code":    405,
            "type":    "MethodNotAllowed",
        }, 405)

    @app.errorhandler(413)
    def handle_payload_too_large(exc):
        return _build_response({
            "message": "Uploaded file exceeds the maximum allowed size.",
            "code":    413,
            "type":    "PayloadTooLarge",
        }, 413)

    # ── Catch-all for unhandled exceptions ────────────────────────────────────
    @app.errorhandler(Exception)
    def handle_unhandled_exception(exc):
        logger.error(
            "[%s] Unhandled exception in %s %s:\n%s",
            _request_id(), request.method, request.path,
            traceback.format_exc()
        )
        return _build_response({
            "message": "An unexpected server error occurred. "
                       "Check the server logs for details.",
            "code":    500,
            "type":    "InternalServerError",
        }, 500)
