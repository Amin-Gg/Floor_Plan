"""Shared stage result contract and central status computation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .findings import Finding, FindingSeverity, FindingStage, Verdict


SCHEMA_QUALITY_STATUSES = {"passed", "passed_with_alerts", "failed"}
COMPLIANCE_STATUSES = {"completed", "completed_with_review", "blocked"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_stage_status(stage: FindingStage | str, findings: Iterable[Finding]) -> str:
    stage = stage if isinstance(stage, FindingStage) else FindingStage(stage)
    rows = list(findings)
    if stage in (FindingStage.SCHEMA, FindingStage.QUALITY):
        if any(f.severity == FindingSeverity.FAIL for f in rows):
            return "failed"
        if rows:
            return "passed_with_alerts"
        return "passed"
    if any(f.verdict == Verdict.NOT_EVALUATED and f.severity == FindingSeverity.FAIL for f in rows):
        return "blocked"
    if any(f.verdict in (Verdict.NEEDS_REVIEW, Verdict.NOT_EVALUATED) for f in rows):
        return "completed_with_review"
    return "completed"


@dataclass
class ValidationResult:
    stage: FindingStage | str
    status: Optional[str] = None
    findings: list[Finding] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime = field(default_factory=utc_now)
    checker_version: str = "stage8-remediation-phase1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.stage, FindingStage):
            self.stage = FindingStage(str(self.stage))
        if self.status is None:
            self.status = compute_stage_status(self.stage, self.findings)
        allowed = COMPLIANCE_STATUSES if self.stage is FindingStage.COMPLIANCE else SCHEMA_QUALITY_STATUSES
        if self.status not in allowed:
            raise ValueError(f"Invalid {self.stage.value} status: {self.status!r}")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")

    @property
    def blocking(self) -> bool:
        return self.status in {"failed", "blocked"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "checker_version": self.checker_version,
            "metadata": dict(self.metadata),
        }
