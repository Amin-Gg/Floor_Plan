"""Plugin executor for the model-quality validation stage."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Optional

from domain.findings import Finding, FindingStage
from domain.model import BuildingModel
from domain.validation import ValidationResult, utc_now
from services.numeric_checker import assign_finding_ordinals

from .context import QualityContext
from .findings import plugin_error_finding
from .protocol import QualityCheck
from .registry import DEFAULT_QUALITY_CHECKS, validate_registry

logger = logging.getLogger(__name__)

QUALITY_CHECKER_VERSION = "quality-stage8-phase5"


def _validated_plugin_output(
    check: QualityCheck,
    rows: object,
) -> list[Finding]:
    if not isinstance(rows, list):
        raise TypeError(f"run() returned {type(rows).__name__}, expected list[Finding]")
    for row in rows:
        if not isinstance(row, Finding):
            raise TypeError(
                f"run() returned {type(row).__name__}, expected Finding"
            )
        if row.stage is not FindingStage.QUALITY:
            raise ValueError(
                f"plugin emitted stage={row.stage.value!r}, expected 'quality'"
            )
        if row.code not in check.codes:
            raise ValueError(
                f"plugin emitted undeclared code {row.code!r}; declared={check.codes!r}"
            )
    return rows


def run_model_quality_checks(
    model: BuildingModel,
    *,
    context: Optional[QualityContext] = None,
    checks: Optional[Sequence[QualityCheck]] = None,
) -> ValidationResult:
    """Run every applicable plugin in deterministic registry order.

    A plugin exception is converted to ``QC-INTERNAL-001`` and does not stop
    later plugins. The plugin's ``blocking`` metadata controls whether that
    internal finding makes the quality stage ``failed`` or merely
    ``passed_with_alerts``.
    """
    started = utc_now()
    ctx = context or QualityContext.from_model(model)
    registry = validate_registry(checks or DEFAULT_QUALITY_CHECKS)
    findings = list(ctx.initial_findings)
    executed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for check in registry:
        try:
            applies = bool(check.applies_to(model, ctx))
        except Exception as exc:  # one broken plugin must not suppress others
            logger.exception("Quality plugin %s failed in applies_to", check.name)
            findings.append(plugin_error_finding(
                model,
                plugin_name=check.name,
                code_prefix=check.code_prefix,
                blocking=check.blocking,
                error=exc,
                phase="applies_to",
            ))
            failed.append(check.name)
            continue

        if not applies:
            skipped.append(check.name)
            continue

        try:
            rows = _validated_plugin_output(check, check.run(model, ctx))
            findings.extend(rows)
            executed.append(check.name)
        except Exception as exc:  # continue with the remaining checks
            logger.exception("Quality plugin %s failed in run", check.name)
            findings.append(plugin_error_finding(
                model,
                plugin_name=check.name,
                code_prefix=check.code_prefix,
                blocking=check.blocking,
                error=exc,
                phase="run",
            ))
            failed.append(check.name)

    assign_finding_ordinals(findings)
    result = ValidationResult(
        stage=FindingStage.QUALITY,
        findings=findings,
        started_at=started,
        completed_at=utc_now(),
        checker_version=QUALITY_CHECKER_VERSION,
        metadata={
            "registry": [check.name for check in registry],
            "executed_checks": executed,
            "skipped_checks": skipped,
            "failed_checks": failed,
            "plugin_count": len(registry),
            "context": dict(ctx.metadata),
        },
    )
    if findings:
        logger.info(
            "L2 quality check: %s (%d finding(s): %s)",
            result.status,
            len(findings),
            ", ".join(sorted({finding.code or "" for finding in findings})),
        )
    else:
        logger.info("L2 quality check: passed")
    return result
