from __future__ import annotations

from services.validation_pipeline import (
    PipelineMode,
    PipelineRequest,
    PipelineSourceType,
    run_validation_pipeline,
)


def test_pipeline_quality_stage_exposes_phase4_plugin_metadata():
    execution = run_validation_pipeline(PipelineRequest(
        source_type=PipelineSourceType.BIM_DATA,
        bim_data={
            "rooms": [],
            "walls": [],
            "doors": [],
            "windows": [],
        },
        mode=PipelineMode.PRECHECK,
    ))
    assert execution.quality["checker_version"] == "quality-stage8-phase5"
    assert execution.quality["metadata"]["registry"] == [
        "contract_read",
        "identity_integrity",
        "space_tagging",
        "required_properties",
        "unit_consistency",
        "storey_consistency",
        "element_confidence",
        "scale_confidence",
        "manual_parameters",
        "opening_placement",
    ]
