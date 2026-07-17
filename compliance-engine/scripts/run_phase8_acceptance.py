"""Run the Phase-8 IFC → validation → report → Full BCF acceptance flow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from reporting.bcf_exporter import validate_bcf_archive
from services.validation_pipeline import PipelineRequest, run_validation_pipeline


def _load_clauses(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Clause corpus must be a JSON array")
    return [item for item in payload if isinstance(item, dict) and not item.get("skip_category")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, default=Path("tests/fixtures/sample_plan.ifc"))
    parser.add_argument("--clauses", type=Path, default=Path("data/mabhas_clauses.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/phase8_acceptance"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    execution = run_validation_pipeline(
        PipelineRequest(
            source_type="ifc",
            ifc_path=str(args.ifc),
            clauses=_load_clauses(args.clauses),
            out_dir=str(args.output_dir),
            metadata={"plan_name": args.ifc.name, "acceptance_phase": 8},
            mode="full_check",
        )
    )
    execution_path = args.output_dir / "pipeline_execution.json"
    execution_path.write_text(
        json.dumps(execution.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    if execution.blocked:
        print(json.dumps({"ok": False, "blocked_reason": execution.blocked_reason}, indent=2))
        return 2

    bcf_path = Path(execution.reports["bcf"] or "")
    from ingest.ifc_io import open_ifc_safely

    source_model = open_ifc_safely(args.ifc)
    source_guids = {
        str(getattr(entity, "GlobalId", "") or "")
        for entity in source_model.by_type("IfcRoot")
    }
    bcf_summary = validate_bcf_archive(
        bcf_path, allowed_ifc_guids=source_guids
    )
    result = {
        "ok": True,
        "schema_status": (execution.schema or {}).get("status"),
        "quality_status": (execution.quality or {}).get("status"),
        "compliance_summary": dict(execution.compliance.summary),
        "bcf": bcf_summary,
        "reports": execution.reports,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
