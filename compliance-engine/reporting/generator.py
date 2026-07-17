"""Authoritative report-bundle generation.

All output formats are rendered from one immutable ValidationReport. This is
the only production report-generation entry point.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from reporting.bcf_exporter import BcfExportPolicy, export_bcf
from reporting.html_report import render_html
from reporting.json_report import write_json_report
from reporting.pdf_report import write_pdf_report
from reporting.report_model import ENGINE_VERSION, build_validation_report


def generate_report_bundle(
    compliance: Optional[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
    output_dir: str = ".",
    coverage: Optional[Dict[str, Any]] = None,
    stages: Optional[Dict[str, Any]] = None,
    *,
    model: Any = None,
    mode: str = "full_check",
    skipped_stages: Optional[Mapping[str, str]] = None,
    engine_version: str = ENGINE_VERSION,
    generated_at: datetime | str | None = None,
    run_id: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    stage_payload = dict(stages or {})
    report = build_validation_report(
        compliance=compliance,
        schema=stage_payload.get("schema"),
        quality=stage_payload.get("quality"),
        model=model,
        metadata=metadata,
        mode=mode,
        skipped_stages=skipped_stages,
        coverage=coverage,
        engine_version=engine_version,
        generated_at=generated_at,
        run_id=run_id,
    )

    json_path = write_json_report(report, target / "compliance_result.json")
    html_text = render_html(report)
    html_path = target / "compliance_report.html"
    html_path.write_text(html_text, encoding="utf-8")
    pdf_path = write_pdf_report(html_text, target / "compliance_report.pdf")
    bcf_path = target / "compliance_issues.bcf"
    bcf_result = export_bcf(
        report,
        model if hasattr(model, "provenance") else None,
        bcf_path,
        policy=BcfExportPolicy(),
    )
    return {
        "html": str(html_path),
        "pdf": pdf_path,
        "bcf": str(bcf_path),
        "bcf_manifest": bcf_result.manifest_path,
        "json": json_path,
    }


__all__ = ["generate_report_bundle"]
