"""CLI for the canonical IFC validation pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from api.pipeline import load_clauses
from services.validation_pipeline import PipelineRequest, run_validation_pipeline

_ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path)
    parser.add_argument("--clauses", type=Path, default=_ROOT / "data" / "mabhas_clauses.json")
    parser.add_argument("--out", type=Path, default=Path("ifc_reports"))
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--manual-inputs", type=Path, default=None)
    parser.add_argument("--precheck", action="store_true")
    parser.add_argument("--no-reports", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    clauses = load_clauses(str(args.clauses), required=not args.precheck)
    manual_inputs = None
    if args.manual_inputs:
        manual_inputs = json.loads(args.manual_inputs.read_text(encoding="utf-8"))

    execution = run_validation_pipeline(PipelineRequest(
        source_type="ifc",
        ifc_path=str(args.ifc),
        clauses=clauses,
        out_dir=None if args.no_reports else str(args.out),
        metadata={"plan_name": args.ifc.name},
        mode="precheck" if args.precheck else "full_check",
        threshold=args.threshold,
        manual_inputs=manual_inputs,
        generate_reports=not args.no_reports,
    ))

    payload = execution.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps({
            "blocked": execution.blocked,
            "schema": (execution.schema or {}).get("status"),
            "quality": (execution.quality or {}).get("status"),
            "summary": dict(execution.compliance.summary) if execution.compliance else {},
            "reports": execution.reports,
        }, ensure_ascii=False, indent=2))

    if execution.blocked:
        return 2
    return 1 if execution.compliance and execution.compliance.summary.get("FAIL", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
