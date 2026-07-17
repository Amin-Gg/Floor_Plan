"""Machine-readable API errors shared by every Stage-1 endpoint."""
from __future__ import annotations

import logging
import traceback
from typing import Any

from flask import g, jsonify, request

logger = logging.getLogger(__name__)


class APIError(Exception):
    status_code = 500
    error_type = "APIError"
    error_code = "internal_error"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.error_code,
            "message": self.message,
            "status": self.status_code,
            "type": self.error_type,
            "details": self.details,
        }


class ValidationError(APIError):
    status_code = 400
    error_type = "ValidationError"
    error_code = "invalid_request"


class ImageValidationError(ValidationError):
    error_type = "ImageValidationError"
    error_code = "invalid_image"


class ModelNotReadyError(APIError):
    status_code = 503
    error_type = "ModelNotReadyError"
    error_code = "model_not_ready"

    def __init__(self):
        super().__init__(
            "The AI model is not initialized. Check /health and the server logs."
        )


class NotFoundError(APIError):
    status_code = 404
    error_type = "NotFoundError"
    error_code = "not_found"


class ConflictError(APIError):
    status_code = 409
    error_type = "ConflictError"
    error_code = "resource_conflict"


class UpstreamServiceError(APIError):
    status_code = 502
    error_type = "UpstreamServiceError"
    error_code = "upstream_protocol_error"


class UpstreamUnavailableError(APIError):
    status_code = 503
    error_type = "UpstreamUnavailableError"
    error_code = "upstream_unavailable"


class GatewayTimeoutError(APIError):
    status_code = 504
    error_type = "GatewayTimeoutError"
    error_code = "upstream_timeout"


def _request_id() -> str:
    return getattr(g, "request_id", "n/a")


def _build_response(error_dict: dict[str, Any], status_code: int):
    return jsonify({
        "success": False,
        "request_id": _request_id(),
        "error": error_dict,
    }), status_code


def openapi_validation_error_response(exc):
    """flask-openapi3 validation callback using the public error envelope."""
    details = []
    try:
        details = exc.errors(include_url=False)
    except Exception:
        details = [{"message": str(exc)}]
    body = {
        "success": False,
        "request_id": _request_id(),
        "error": {
            "code": "schema_validation_failed",
            "message": "Request payload failed schema validation.",
            "status": 422,
            "type": "RequestValidationError",
            "details": {"violations": details},
        },
    }
    response = jsonify(body)
    response.status_code = 422
    return response


def register_error_handlers(app) -> None:
    @app.errorhandler(APIError)
    def handle_api_error(exc: APIError):
        log = logger.error if exc.status_code >= 500 else logger.warning
        log("[%s] %s: %s", _request_id(), exc.error_type, exc.message)
        return _build_response(exc.to_dict(), exc.status_code)

    def standard(status: int, code: str, message: str, error_type: str):
        return _build_response({
            "code": code,
            "message": message,
            "status": status,
            "type": error_type,
            "details": {},
        }, status)

    @app.errorhandler(400)
    def handle_bad_request(_exc):
        return standard(400, "bad_request", "Malformed request.", "BadRequest")

    @app.errorhandler(404)
    def handle_not_found(_exc):
        return standard(
            404, "endpoint_not_found",
            f"Endpoint not found: {request.method} {request.path}", "NotFound",
        )

    @app.errorhandler(405)
    def handle_method_not_allowed(_exc):
        return standard(
            405, "method_not_allowed",
            f"Method {request.method} is not allowed on {request.path}.",
            "MethodNotAllowed",
        )

    @app.errorhandler(413)
    def handle_payload_too_large(_exc):
        return standard(
            413, "payload_too_large",
            "Uploaded payload exceeds the configured limit.", "PayloadTooLarge",
        )

    @app.errorhandler(Exception)
    def handle_unhandled_exception(_exc):
        logger.error(
            "[%s] Unhandled exception in %s %s:\n%s",
            _request_id(), request.method, request.path, traceback.format_exc(),
        )
        return standard(
            500, "internal_error",
            "An unexpected server error occurred.", "InternalServerError",
        )
