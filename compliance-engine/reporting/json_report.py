"""JSON serialization and schema validation for Validation Report v1.0."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .report_model import ValidationReport

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "validation_report_v1.schema.json"


def load_report_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_report_dict(data: Mapping[str, Any]) -> None:
    validator = Draft202012Validator(
        load_report_schema(),
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
    if errors:
        lines = []
        for error in errors[:20]:
            location = ".".join(str(x) for x in error.absolute_path) or "<root>"
            lines.append(f"{location}: {error.message}")
        if len(errors) > 20:
            lines.append(f"... and {len(errors) - 20} more schema error(s)")
        raise ValueError("Validation report does not match v1 schema:\n" + "\n".join(lines))


def write_json_report(report: ValidationReport, path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    validate_report_dict(payload)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return str(target)


__all__ = ["load_report_schema", "validate_report_dict", "write_json_report"]
