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

from typing import Any, Literal, Optional
from pydantic_core import core_schema
from pydantic import BaseModel, Field, field_validator, model_validator


class APIFileStorage:
    """Pydantic-compatible Werkzeug upload type for flask-openapi3/Pydantic 2.12+."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        def validate(value):
            if value is None or not hasattr(value, "read") or not hasattr(value, "filename"):
                raise ValueError("uploaded file is required")
            return value
        return core_schema.no_info_plain_validator_function(validate)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        return {"type": "string", "format": "binary"}


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

    @model_validator(mode="after")
    def _elements_fit_within_wall(self) -> "BuildingParams":
        """Cross-field consistency: openings must fit under the wall height.

        wall_height is defined as finished floor level → underside of the
        slab/ceiling above, so a window head (sill + window height) or a door
        taller than the wall is physically impossible and would produce a
        geometrically invalid IFC.
        """
        head = self.window_sill_height + self.window_height
        if head > self.wall_height:
            raise ValueError(
                f"window_sill_height + window_height ({head:.0f} mm) exceeds "
                f"wall_height ({self.wall_height:.0f} mm) — the window head "
                f"would be above the ceiling."
            )
        if self.door_height > self.wall_height:
            raise ValueError(
                f"door_height ({self.door_height:.0f} mm) exceeds wall_height "
                f"({self.wall_height:.0f} mm)."
            )
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Request schemas
# ─────────────────────────────────────────────────────────────────────────────

class AnalyzeFormRequest(BaseModel):
    """Documented multipart fields for POST /analyze.

    Runtime validation is performed by the same strict contract adapters used
    by direct exporter calls; these fields keep OpenAPI aligned with that public
    boundary.
    """
    image: APIFileStorage = Field(
        description="Floor-plan image (PNG/JPEG/WebP/TIFF).",
    )
    scale_factor_mm_per_pixel: float = Field(
        default=1.0, ge=0.01, le=100.0,
        description="Legacy scalar fallback. Without scale_evidence it is classified as default_unverified.",
        examples=[1.0, 0.5, 25.0],
    )
    scale_evidence: Optional[dict] = Field(
        default=None,
        description="Versioned Scale Evidence v1.0 payload; see contracts/scale_evidence_v1.json.",
    )
    manual_inputs: Optional[dict] = Field(
        default=None,
        description="Strict Manual Inputs v1.0 payload; see contracts/manual_inputs_v1.json.",
    )

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    @field_validator("scale_factor_mm_per_pixel", mode="before")
    @classmethod
    def coerce_scale_factor(cls, v):
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ValueError(
                f"scale_factor_mm_per_pixel must be a number, got: {v!r}"
            )


class IFCMetadata(BaseModel):
    project_name: str = "Floor Plan Project"
    project_address: str = ""
    building_name: str = "Building"
    storey_name: str = "Ground Floor"
    storey_elevation: float = Field(default=0.0, ge=-5000, le=50000)

    model_config = {"extra": "forbid"}



# ─────────────────────────────────────────────────────────────────────────────
# Response schemas (used for OpenAPI documentation)
# ─────────────────────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable error description.")
    status: int = Field(description="HTTP status code.")
    type: str = Field(description="Exception category.")
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    request_id: str
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    environment: str
    python_version: str
    model_path: str
    model_error: Optional[str] = None
    compliance_engine: Optional[dict[str, Any]] = None


class AnalyzeResponse(BaseModel):
    success: Literal[True] = True
    request_id: str
    message: str
    analysis_file: str
    visualization_file: str
    bim_data: dict[str, Any]
    summary: dict[str, Any]
    analysis_report: dict[str, Any]
    image_processing: dict[str, Any]


class ExportIFCRequest(BaseModel):
    analysis_file: str = Field(description="Filename returned by POST /analyze.")
    manual_inputs: Optional[dict[str, Any]] = None
    ifc_metadata: IFCMetadata = Field(default_factory=IFCMetadata)
    validate_only: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("analysis_file", mode="before")
    @classmethod
    def sanitize_required_filename(cls, value):
        value = str(value or "").strip()
        if not value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("analysis_file must be a plain filename")
        return value


class ExportIFCUploadForm(BaseModel):
    bim_json: APIFileStorage
    manual_inputs: Optional[dict[str, Any]] = None
    ifc_metadata: Optional[dict[str, Any]] = None
    validate_only: bool = False

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}


class ComplianceFromAnalysisRequest(BaseModel):
    analysis_file: str
    plan_name: Optional[str] = None
    manual_inputs: Optional[dict[str, Any]] = None
    ifc_metadata: IFCMetadata = Field(default_factory=IFCMetadata)

    model_config = {"extra": "forbid"}

    @field_validator("analysis_file", mode="before")
    @classmethod
    def sanitize_analysis_file(cls, value):
        value = str(value or "").strip()
        if not value or "/" in value or "\\" in value or ".." in value:
            raise ValueError("analysis_file must be a plain filename")
        return value


class ComplianceIFCForm(BaseModel):
    ifc_file: APIFileStorage
    plan_name: Optional[str] = None
    manual_inputs: Optional[dict[str, Any]] = None

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}


class ComplianceJobResponse(BaseModel):
    success: Literal[True] = True
    request_id: str
    correlation_id: str
    job_id: str
    status: str
    status_url: str
    reports: dict[str, str]


class ComplianceJobStatusResponse(BaseModel):
    success: Literal[True] = True
    request_id: str
    correlation_id: str
    job: dict[str, Any]


class ComplianceWaitQuery(BaseModel):
    timeout_seconds: float = Field(default=60.0, ge=0.1, le=300.0)
    poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=10.0)


class JobPath(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{12}$")


class ReportPath(JobPath):
    kind: Literal["json", "html", "pdf", "bcf"]
