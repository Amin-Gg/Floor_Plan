"""
utils/validators.py
===================
Reusable request validation helpers for the FloorPlanTo3D Flask API.

All functions raise ValidationError or ImageValidationError (both subclasses
of APIError) on bad input.  The registered error handler converts these to
400 responses automatically — no try/except needed in routes.

Usage in a route
----------------
    from utils.validators import (
        require_image_upload,
        validate_scale_factor,
        validate_building_params,
    )

    image = require_image_upload()                        # raises on missing/bad file
    scale  = validate_scale_factor(                       # raises on bad value
        request.form.get("scale_factor_mm_per_pixel", 1.0)
    )
    params = validate_building_params(body.get("building_params", {}))
"""

import logging
from typing import Optional

from flask import request
from PIL import Image

from utils.error_handlers import ValidationError, ImageValidationError

# Prevent decompression bomb attacks — images that appear small but expand to
# gigabytes in memory (e.g. a 1x1 pixel PNG with embedded 1 GB data).
# Default PIL limit is 178 million pixels; we keep a conservative 100 MP cap.
Image.MAX_IMAGE_PIXELS = 100_000_000   # 100 megapixels

logger = logging.getLogger(__name__)

# Allowed image MIME types
_ALLOWED_MIME = {
    "image/jpeg", "image/jpg", "image/png",
    "image/bmp", "image/tiff", "image/webp"
}

# Building parameter bounds  (min, max, type)
_PARAM_RULES: dict = {
    "wall_height":        (500,   6000,  float, "mm"),
    "floor_thickness":    (50,    600,   float, "mm"),
    "door_height":        (1800,  3000,  float, "mm"),
    "window_sill_height": (0,     2000,  float, "mm"),
    "window_height":      (200,   3000,  float, "mm"),
    "storey_elevation":   (-5000, 50000, float, "mm"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Image upload
# ─────────────────────────────────────────────────────────────────────────────

def require_image_upload(field_name: str = "image") -> Image.Image:
    """
    Validate and open the uploaded image from request.files.

    Checks:
        1. The field exists in the multipart form
        2. A filename was provided (i.e. a file was actually selected)
        3. The file can be opened as an image by Pillow

    Parameters
    ----------
    field_name : str
        The form field name expected to contain the image. Default: "image".

    Returns
    -------
    PIL.Image.Image
        The opened image object.

    Raises
    ------
    ImageValidationError
        If the field is missing, empty, or the file is not a valid image.
    """
    if field_name not in request.files:
        raise ImageValidationError(
            f"Required file field '{field_name}' is missing from the request.",
            details={
                "expected_field": field_name,
                "content_type":   request.content_type,
                "hint": f"Send a multipart/form-data POST with a file field named '{field_name}'.",
            }
        )

    upload = request.files[field_name]

    if not upload or upload.filename == "":
        raise ImageValidationError(
            f"File field '{field_name}' is present but no file was selected.",
            details={"field": field_name}
        )

    try:
        img = Image.open(upload.stream)
        img.verify()                    # detects truncated / corrupt files
        upload.stream.seek(0)           # verify() consumes the stream — rewind
        img = Image.open(upload.stream) # re-open after rewind
        return img
    except Exception as exc:
        raise ImageValidationError(
            f"The uploaded file is not a valid image: {exc}",
            details={
                "filename":      upload.filename,
                "content_type":  upload.content_type,
                "allowed_types": sorted(_ALLOWED_MIME),
            }
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# Numeric parameters
# ─────────────────────────────────────────────────────────────────────────────

def validate_scale_factor(value, min_val: float = 0.01,
                          max_val: float = 100.0) -> float:
    """
    Validate the scale_factor_mm_per_pixel form parameter.

    Parameters
    ----------
    value
        Raw value from request.form.get(...).  May be a string.
    min_val, max_val : float
        Inclusive bounds.  Default: [0.01, 100.0] mm/px.

    Returns
    -------
    float
        Validated scale factor.

    Raises
    ------
    ValidationError
        If the value is not a number or is out of range.
    """
    try:
        val = float(value)
    except (TypeError, ValueError):
        raise ValidationError(
            f"scale_factor_mm_per_pixel must be a number, got: {value!r}",
            details={"received": value, "expected": "float"}
        )

    if not (min_val <= val <= max_val):
        raise ValidationError(
            f"scale_factor_mm_per_pixel must be between {min_val} and {max_val}, "
            f"got {val}.",
            details={"received": val, "min": min_val, "max": max_val, "unit": "mm/pixel"}
        )

    return val


def validate_building_params(raw: dict) -> dict:
    """
    Validate and coerce the building_params dict for the /export/ifc endpoint.

    Only the keys present in `raw` are validated — absent keys are left out
    (the exporter will apply its own defaults for those).

    Parameters
    ----------
    raw : dict
        The building_params dict from the request body. May be empty.

    Returns
    -------
    dict
        Cleaned and type-coerced params dict.

    Raises
    ------
    ValidationError
        If any numeric param is the wrong type or out of range.
    """
    if not isinstance(raw, dict):
        raise ValidationError(
            "building_params must be a JSON object (dict), "
            f"got {type(raw).__name__}.",
            details={"received_type": type(raw).__name__}
        )

    cleaned: dict = {}

    # String params — just pass through, strip whitespace
    for key in ("project_name", "project_address", "building_name", "storey_name"):
        if key in raw:
            cleaned[key] = str(raw[key]).strip()

    # Numeric params — validate type and range
    for key, (lo, hi, cast, unit) in _PARAM_RULES.items():
        if key not in raw:
            continue
        raw_val = raw[key]
        try:
            val = cast(raw_val)
        except (TypeError, ValueError):
            raise ValidationError(
                f"building_params.{key} must be a number, got: {raw_val!r}",
                details={"field": key, "received": raw_val}
            )
        if not (lo <= val <= hi):
            raise ValidationError(
                f"building_params.{key} must be between {lo} and {hi} {unit}, "
                f"got {val}.",
                details={"field": key, "received": val, "min": lo, "max": hi, "unit": unit}
            )
        cleaned[key] = val

    # Warn about unknown keys (don't raise — be lenient with extra data)
    known = set(_PARAM_RULES) | {"project_name", "project_address",
                                  "building_name", "storey_name"}
    unknown = set(raw) - known
    if unknown:
        logger.warning("building_params contains unknown keys (ignored): %s", unknown)

    return cleaned
