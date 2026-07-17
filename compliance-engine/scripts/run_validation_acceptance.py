"""Run the final Phase-9 end-to-end acceptance scenario.

The script starts from a real IFC file, validates its IFC schema, converts it to
our canonical BuildingModel, applies deterministic fixture defects, merges
Manual Inputs v1.0, runs Quality and Compliance, and writes JSON/HTML/PDF/BCF
from one ValidationReport.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.elements import Window
from domain.geometry import OpeningPlacement, Point2D, Polygon2D
from domain.identifiers import build_element_identity
from ingest.ifc_io import open_ifc_safely
from ingest.ifc_to_bim_data import ifc_to_building_model
from reporting.bcf_exporter import validate_bcf_archive
from reporting.generator import generate_report_bundle
from services.validation_pipeline import PipelineRequest, run_validation_pipeline
from validation.schema import validate_ifc_schema_context


@dataclass(frozen=True)
class AcceptanceResult:
    summary: dict[str, Any]
    output_dir: Path


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_clauses(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if not isinstance(payload, list):
        raise ValueError("Clause corpus must be a JSON array")
    return [
        item for item in payload
        if isinstance(item, dict) and not item.get("skip_category")
    ]


def _display_id(element: Any) -> str:
    identity = element.identity
    return identity.source_id or identity.ifc_guid or identity.internal_id


def _build_acceptance_model(ifc_path: Path):
    """Create the documented acceptance model from the real sample IFC.

    Deliberate fixture conditions:
      * one malformed Space with missing area and an open boundary;
      * the original IFC Window extends beyond its host Wall endpoints;
      * a second synthetic Window receives different manual dimensions.

    The original IFC entities retain their real GlobalIds so BCF selection is
    still verified against the source file. The synthetic window intentionally
    has no IFC GUID and therefore cannot create a model-addressable BCF topic.
    """
    schema_result, parsed = validate_ifc_schema_context(str(ifc_path))
    if schema_result.blocking or parsed is None:
        raise RuntimeError(
            f"Acceptance source IFC failed schema validation: {schema_result.to_dict()}"
        )
    model = ifc_to_building_model(str(ifc_path), parsed_model=parsed.model)

    if not model.windows or not model.walls or len(model.spaces) < 2:
        raise RuntimeError("Acceptance source lacks the required model elements")

    # Real IFC window Wb: keep its IFC GlobalId but force an endpoint overflow.
    overflow_window = next(
        (window for window in model.windows if window.identity.source_id == "Wb"),
        model.windows[0],
    )
    host = next(
        wall for wall in model.walls
        if _display_id(wall) == str(overflow_window.host_wall_id)
    )
    wall_length = (
        ((host.end.x - host.start.x) ** 2 + (host.end.y - host.start.y) ** 2) ** 0.5
        if host.start is not None and host.end is not None else 3000.0
    )
    overflow_window.placement = OpeningPlacement(
        center_offset_mm=max(100.0, wall_length - 100.0),
        source_convention="center",
    )

    # Add a second, valid window. Its dimensions are resolved by manual inputs.
    second_host = next(
        (wall for wall in model.walls if wall.identity.source_id == "Wbk"),
        model.walls[0],
    )
    if second_host.start is not None and second_host.end is not None:
        insertion = Point2D(
            (second_host.start.x + second_host.end.x) / 2.0,
            (second_host.start.y + second_host.end.y) / 2.0,
        )
        length = (
            (second_host.end.x - second_host.start.x) ** 2
            + (second_host.end.y - second_host.start.y) ** 2
        ) ** 0.5
        center_offset = length / 2.0
    else:
        insertion = Point2D(0.0, 0.0)
        center_offset = 1000.0

    synthetic_identity = build_element_identity(
        model_fingerprint=model.provenance.model_fingerprint,
        element_type="window",
        source_type="acceptance_fixture",
        source_id="W-ACCEPT-02",
        model_name=model.provenance.model_name,
    )
    model.windows.append(Window(
        identity=synthetic_identity,
        storey_id=second_host.storey_id,
        width_mm=None,
        height_mm=None,
        sill_height_mm=None,
        host_wall_id=_display_id(second_host),
        insertion_point=insertion,
        insertion_z_mm=1000.0,
        placement=OpeningPlacement(center_offset, "center"),
        is_exterior=True,
        width_source="manual",
        provenance={"fixture": "phase9_acceptance"},
    ))

    # Deliberately malformed room: missing area and open boundary.
    malformed = next(
        (space for space in model.spaces if space.identity.source_id == "Rbath"),
        model.spaces[-1],
    )
    malformed.area_m2 = None
    if malformed.boundary is not None:
        ring = malformed.boundary.points
        if len(ring) >= 4 and ring[0] == ring[-1]:
            malformed.boundary = Polygon2D(tuple(ring[:-1]))
        elif len(ring) >= 3:
            malformed.boundary = Polygon2D(tuple(ring))
    malformed.provenance["fixture_defect"] = "missing_area_and_open_boundary"

    return schema_result, model


def run_acceptance(
    *,
    ifc_path: Path,
    manual_inputs_path: Path,
    clauses_path: Path,
    output_dir: Path,
) -> AcceptanceResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    schema_result, model = _build_acceptance_model(ifc_path)
    manual_inputs = _load_json(manual_inputs_path)
    clauses = _load_clauses(clauses_path)

    execution = run_validation_pipeline(PipelineRequest(
        source_type="building_model",
        building_model=model,
        manual_inputs=manual_inputs,
        clauses=clauses,
        metadata={
            "plan_name": ifc_path.name,
            "acceptance_phase": 9,
            "acceptance_fixture": "final_remediation_acceptance",
        },
        mode="full_check",
        generate_reports=False,
    ))
    if execution.blocked or execution.compliance is None or execution.building_model is None:
        raise RuntimeError(f"Acceptance pipeline was blocked: {execution.blocked_reason}")

    reports = generate_report_bundle(
        execution.compliance.to_dict(),
        {
            "plan_name": ifc_path.name,
            "acceptance_phase": 9,
            "acceptance_fixture": "final_remediation_acceptance",
        },
        output_dir=str(output_dir),
        coverage=execution.coverage,
        stages={
            "schema": schema_result.to_dict(),
            "quality": execution.quality,
        },
        model=execution.building_model,
        mode="full_check",
        skipped_stages=execution.skipped_stages,
    )
    execution.reports = reports
    # The acceptance run is the release gate: municipalities receive the full
    # artifact set, so a silently skipped PDF (WeasyPrint unavailable) must
    # fail the run loudly here even though ordinary pipeline runs degrade
    # gracefully. This resolves the runtime-optional / acceptance-mandatory
    # inconsistency found in the final independent review.
    required = ("json", "html", "pdf", "bcf")
    missing = []
    for kind in required:
        raw_path = reports.get(kind)
        artifact = Path(raw_path) if raw_path else None
        if artifact is None or not artifact.is_file() or artifact.stat().st_size == 0:
            missing.append(kind)
    if missing:
        raise RuntimeError(
            "Acceptance requires the complete artifact set; missing: "
            f"{', '.join(missing)}. For PDF, install WeasyPrint and its "
            "system libraries (pango/cairo) — see requirements.txt and the "
            "Dockerfile."
        )
    execution.stage_trace.insert(1, "schema")
    execution.stage_trace.append("reporting")

    quality_codes = {
        str(item.get("code"))
        for item in (execution.quality or {}).get("findings", [])
    }
    required_quality_codes = {"QC-SPACE-004", "QC-SPACE-006", "QC-PLACE-007"}
    missing_codes = required_quality_codes - quality_codes
    if missing_codes:
        raise AssertionError(f"Acceptance fixture did not trigger {sorted(missing_codes)}")

    window_values = {
        _display_id(window): {
            "width_mm": window.width_mm,
            "height_mm": window.height_mm,
            "sill_height_mm": window.sill_height_mm,
        }
        for window in execution.building_model.windows
    }
    if window_values.get("Wb", {}).get("width_mm") != 1400.0:
        raise AssertionError("Wb manual override was not applied")
    if window_values.get("W-ACCEPT-02", {}).get("width_mm") != 900.0:
        raise AssertionError("W-ACCEPT-02 manual override was not applied")

    compliance_summary = dict(execution.compliance.summary)
    if compliance_summary.get("FAIL", 0) < 1:
        raise AssertionError("Acceptance fixture must contain at least one real FAIL")
    if compliance_summary.get("NOT_EVALUATED", 0) < 1:
        raise AssertionError("Acceptance fixture must contain at least one NOT_EVALUATED")

    source_model = open_ifc_safely(ifc_path)
    source_guids = {
        str(getattr(entity, "GlobalId", "") or "")
        for entity in source_model.by_type("IfcRoot")
    }
    bcf_summary = validate_bcf_archive(
        Path(reports["bcf"] or ""),
        allowed_ifc_guids=source_guids,
    )

    execution_path = output_dir / "pipeline_execution.json"
    execution_path.write_text(
        json.dumps(execution.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    result = {
        "ok": True,
        "schema_status": schema_result.status,
        "quality_status": (execution.quality or {}).get("status"),
        "quality_codes": sorted(quality_codes),
        "compliance_summary": compliance_summary,
        "window_values": window_values,
        "bcf": bcf_summary,
        "reports": reports,
        "stage_trace": execution.stage_trace,
    }
    result_path = output_dir / "acceptance_result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return AcceptanceResult(summary=result, output_dir=output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, default=Path("tests/fixtures/sample_plan.ifc"))
    parser.add_argument(
        "--manual-inputs",
        type=Path,
        default=Path("tests/fixtures/remediation_manual_inputs.json"),
    )
    parser.add_argument("--clauses", type=Path, default=Path("data/mabhas_clauses.json"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/remediation_acceptance"),
    )
    args = parser.parse_args()
    result = run_acceptance(
        ifc_path=args.ifc,
        manual_inputs_path=args.manual_inputs,
        clauses_path=args.clauses,
        output_dir=args.output_dir,
    )
    print(json.dumps(result.summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
