"""
validation/report.py
=====================
Shared data structures for the model-standards validator.

Severity policy (chosen for this project): "block on critical, warn on minor".
  - CRITICAL → the output is not fit for purpose; the caller should refuse it.
  - WARNING  → the output is usable but imperfect; surface it, don't block.
  - INFO     → advisory / a check could not be run; never blocks.

`ValidationReport.blocked` is True iff at least one CRITICAL issue exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# Which validation layer a check belongs to (matches the four requested layers).
LAYER_IFC4 = "ifc4_validity"
LAYER_COMPLETENESS = "bim_completeness"
LAYER_GEOMETRY = "geometric_sanity"
LAYER_CODE_READINESS = "code_readiness"


@dataclass
class Issue:
    code: str                       # stable machine code, e.g. "GEOM.WALL.ZERO_LENGTH"
    severity: Severity
    layer: str
    message: str
    element: Optional[str] = None    # element id this issue concerns, if any

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class ValidationReport:
    """Collects issues from one validation stage ('pre_export' or 'post_export')."""
    stage: str                                  # "pre_export" | "post_export"
    issues: List[Issue] = field(default_factory=list)
    checked: Dict[str, int] = field(default_factory=dict)   # counts of what was inspected

    # ── recording ────────────────────────────────────────────────────────────
    def add(self, code: str, severity: Severity, layer: str,
            message: str, element: Optional[str] = None) -> None:
        self.issues.append(Issue(code, severity, layer, message, element))

    def critical(self, code: str, layer: str, message: str,
                 element: Optional[str] = None) -> None:
        self.add(code, Severity.CRITICAL, layer, message, element)

    def warn(self, code: str, layer: str, message: str,
             element: Optional[str] = None) -> None:
        self.add(code, Severity.WARNING, layer, message, element)

    def info(self, code: str, layer: str, message: str,
             element: Optional[str] = None) -> None:
        self.add(code, Severity.INFO, layer, message, element)

    # ── queries ──────────────────────────────────────────────────────────────
    @property
    def n_critical(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.CRITICAL)

    @property
    def n_warning(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.WARNING)

    @property
    def blocked(self) -> bool:
        return self.n_critical > 0

    @property
    def status(self) -> str:
        if self.blocked:
            return "blocked"
        if self.n_warning > 0:
            return "warn"
        return "pass"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "blocked": self.blocked,
            "counts": {"critical": self.n_critical, "warning": self.n_warning,
                       "info": sum(1 for i in self.issues if i.severity is Severity.INFO)},
            "checked": self.checked,
            "issues": [i.to_dict() for i in self.issues],
        }


def merge_reports(stage: str, *reports: ValidationReport) -> Dict[str, Any]:
    """Combine multiple stage reports into one envelope for the API response."""
    all_issues: List[Issue] = []
    checked: Dict[str, int] = {}
    sub: List[Dict[str, Any]] = []
    for r in reports:
        all_issues.extend(r.issues)
        for k, v in r.checked.items():
            checked[k] = checked.get(k, 0) + v
        sub.append(r.to_dict())
    n_crit = sum(1 for i in all_issues if i.severity is Severity.CRITICAL)
    n_warn = sum(1 for i in all_issues if i.severity is Severity.WARNING)
    n_info = sum(1 for i in all_issues if i.severity is Severity.INFO)
    return {
        "stage": stage,
        "status": "blocked" if n_crit else ("warn" if n_warn else "pass"),
        "blocked": n_crit > 0,
        "counts": {"critical": n_crit, "warning": n_warn, "info": n_info},
        "checked": checked,
        "stages": sub,
    }


class IfcContractError(Exception):
    """Raised by the exporter's §A7 gate when the written IFC violates the
    contract. Carries the ValidationReport so callers can surface which
    assertion failed."""

    def __init__(self, message: str, report: "ValidationReport" = None):
        super().__init__(message)
        self.report = report

    def report_dict(self) -> Dict[str, Any]:
        return self.report.to_dict() if self.report is not None else {}
