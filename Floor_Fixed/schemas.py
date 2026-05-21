"""
schemas.py
==========
Pydantic v2 request and response schemas for the FloorPlanTo3D API.

These schemas serve two purposes:
    1. Input validation — Pydantic validates and coerces all incoming JSON
       fields automatically. Invalid data raises ValidationError which the
       error handler converts to HTTP 422 Unprocessable Entity.
    2. OpenAPI documentation — flask-openapi3 reads these schemas to generate
       the Swagger UI at /openapi/swagger automatically. Every field
       description, type, default, and example appears in the docs with no
       extra work.

Usage in a route
----------------
    from schemas import AnalyzeRequest, ExportIFCRequest

    @bp.post("/analyze")
    def analyze(form: AnalyzeRequest):
        ...

    @bp.post("/export/ifc")
    def export_ifc(body: ExportIFCRequest):
        ...
"""

from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Shared sub-schemas
# ─────────────────────────────────────────────────────────────────────────────

class BuildingParams(BaseModel):
    """
    Optional building height and thickness parameters.
    All values are in millimetres.
    If omitted, the system uses the defaults shown below.
    """
    project_name: str = Field(
        default="Floor Plan Project",
        description="Project name stored in the IFC file header and compliance reports."
    )
    project_address: str = Field(
        default="",
        description="Building postal address (optional, for permit filing records)."
    )
    building_name: str = Field(
        default="Building",
        description="Name of the IfcBuilding entity."
    )
    storey_name: str = Field(
        default="Ground Floor",
        description="Name of the floor storey (e.g. 'Ground Floor', 'First Floor')."
    )
    storey_elevation: float = Field(
        default=0.0, ge=-5000, le=50000,
        description="Elevation of this floor above site datum in mm. 0 for ground floor."
    )
    wall_height: float = Field(
        default=2800.0, ge=500, le=6000,
        description="Clear wall height from finished floor level to underside of slab in mm. "
                    "Typical Iranian residential: 2800. Commercial: 3000-3600.",
        examples=[2800, 3000, 3200]
    )
    floor_thickness: float = Field(
        default=200.0, ge=50, le=600,
        description="Structural slab thickness in mm. Typical: 200.",
        examples=[150, 200, 250]
    )
    door_height: float = Field(
        default=2100.0, ge=1800, le=3000,
        description="Clear door opening height in mm. Standard: 2100.",
        examples=[2000, 2100, 2400]
    )
    window_sill_height: float = Field(
        default=900.0, ge=0, le=2000,
        description="Height from finished floor to bottom of window opening in mm. "
                    "Standard residential: 900. Kitchen: 1050.",
        examples=[700, 900, 1050]
    )
    window_height: float = Field(
        default=1200.0, ge=200, le=3000,
        description="Clear window opening height in mm. Standard: 1200. "
                    "Head height = sill_height + window_height.",
        examples=[1000, 1200, 1400]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Request schemas
# ─────────────────────────────────────────────────────────────────────────────

class AnalyzeFormRequest(BaseModel):
    """
    Form fields for POST /analyze (multipart/form-data).
    The image file itself is handled by Flask's request.files — only the
    scalar parameters are validated here.
    """
    scale_factor_mm_per_pixel: float = Field(
        default=1.0, ge=0.01, le=100.0,
        description="How many millimetres one pixel represents in the physical drawing. "
                    "Calculated as: (known dimension in mm) / (dimension in pixels). "
                    "Example: a wall known to be 5000 mm spanning 200 px → factor = 25.0",
        examples=[1.0, 0.5, 25.0]
    )
    building_params: Optional[BuildingParams] = Field(
        default=None,
        description="Optional building height overrides. If omitted, defaults are used."
    )

    @field_validator("scale_factor_mm_per_pixel", mode="before")
    @classmethod
    def coerce_scale_factor(cls, v):
        """Accept string values from multipart form fields."""
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError(
                f"scale_factor_mm_per_pixel must be a number, got: {v!r}"
            )


class ExportIFCRequest(BaseModel):
    """Request body for POST /export/ifc."""
    analysis_file: Optional[str] = Field(
        default=None,
        description="Filename returned by /analyze (e.g. 'final5.json'). "
                    "Required when not uploading a bim_json file directly.",
        examples=["final5.json", "analysis_20250508_142301.json"]
    )
    building_params: BuildingParams = Field(
        default_factory=BuildingParams,
        description="Building height and project metadata parameters."
    )

    @field_validator("analysis_file", mode="before")
    @classmethod
    def sanitize_filename(cls, v):
        """Prevent path traversal attacks."""
        if v is None:
            return v
        v = str(v).strip()
        if "/" in v or "\\" in v or ".." in v:
            raise ValueError(
                "analysis_file must be a filename only, not a path. "
                f"Got: {v!r}"
            )
        return v


# ─────────────────────────────────────────────────────────────────────────────
# Response schemas (used for OpenAPI documentation)
# ─────────────────────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    message: str = Field(description="Human-readable error description.")
    code:    int  = Field(description="HTTP status code.")
    type:    str  = Field(description="Error class name for programmatic handling.")
    details: dict = Field(default_factory=dict, description="Additional context.")


class ErrorResponse(BaseModel):
    success:    bool        = Field(default=False)
    request_id: str         = Field(description="Short ID for correlating with server logs.")
    error:      ErrorDetail


class HealthResponse(BaseModel):
    status:        str  = Field(description="'healthy' or 'degraded'.")
    model_loaded:  bool
    model_name:    str
    num_classes:   int
    gpu_available: bool
    version:       str = Field(default="2.0")


class AnalyzeResponse(BaseModel):
    """Simplified response envelope — full bim_data structure is too large to enumerate here."""
    success:    bool = True
    request_id: str
    bim_data:   dict = Field(description="Full BIM vector data including walls, doors, windows, rooms.")
    summary:    dict = Field(description="Element counts and total areas.")
