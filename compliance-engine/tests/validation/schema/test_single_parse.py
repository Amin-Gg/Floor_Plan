from pathlib import Path

import validation.schema.checker as checker
from services.validation_pipeline import PipelineRequest, run_validation_pipeline


FIXTURE = Path(__file__).parents[2] / "fixtures" / "sample_plan.ifc"


def test_pipeline_opens_ifc_exactly_once(monkeypatch):
    calls = 0
    original = checker.open_ifc_safely

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(checker, "open_ifc_safely", counted)
    execution = run_validation_pipeline(PipelineRequest(
        source_type="ifc",
        ifc_path=str(FIXTURE),
        mode="precheck",
        generate_reports=False,
    ))
    assert execution.blocked is False
    assert execution.parsed_source is not None
    assert execution.parsed_source.model is not None
    assert calls == 1
    assert execution.schema["metadata"]["single_parse_context"] is True
