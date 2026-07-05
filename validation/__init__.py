"""
validation/ — model-standards validator for the FloorPlanTo3D output.

Two stages, matching the requested "pre-export gate + post-export verify":

    from validation import validate_bim_data, validate_ifc_file, merge_reports

    pre = validate_bim_data(bim_data, building_params)   # geometry/completeness/code-readiness
    if pre.blocked:                                       # block on critical
        ...refuse...
    bim_json_to_ifc(bim_data, building_params, path)
    post = validate_ifc_file(path)                        # IFC4 validity + graph completeness
    if post.blocked:
        ...refuse + delete file...
    envelope = merge_reports("export", pre, post)         # combined report for the response
"""

from .report import (
    Severity, Issue, ValidationReport, merge_reports, IfcContractError,
    LAYER_IFC4, LAYER_COMPLETENESS, LAYER_GEOMETRY, LAYER_CODE_READINESS,
)
from .bim_checks import validate_bim_data
from .ifc_checks import validate_ifc_file, validate_ifc_contract

__all__ = [
    "Severity", "Issue", "ValidationReport", "merge_reports", "IfcContractError",
    "validate_bim_data", "validate_ifc_file", "validate_ifc_contract",
    "LAYER_IFC4", "LAYER_COMPLETENESS", "LAYER_GEOMETRY", "LAYER_CODE_READINESS",
]
