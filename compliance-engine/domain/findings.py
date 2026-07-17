"""Shared finding contract for schema, quality and compliance stages.

The class deliberately retains the Stage-8 constructor/serialization fields so
existing deterministic agents remain unchanged during Phase 1.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

SIMSYS_FINDING_NAMESPACE = uuid.UUID("0b35492f-57f1-54ff-a682-965ed29c1a3c")
_US = "\x1f"


class FindingStage(str, Enum):
    SCHEMA = "schema"
    QUALITY = "quality"
    COMPLIANCE = "compliance"


class FindingSeverity(str, Enum):
    FAIL = "fail"
    ALERT = "alert"
    INFO = "info"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


@dataclass
class Finding:
    # Stage-8 compatibility fields. Existing agents instantiate these directly.
    article_id: str
    verdict: Verdict
    message: str
    object: Optional[str] = None
    measured: Optional[float] = None
    required: Optional[Any] = None
    unit: Optional[str] = None
    element_id: Optional[str] = None
    rule_text_en: Optional[str] = None
    category: str = FindingStage.COMPLIANCE.value
    code: Optional[str] = None
    expected: Optional[Any] = None
    actual: Optional[Any] = None
    ordinal: int = 0
    unsupported: bool = False

    # Shared Phase-1 fields.
    severity: FindingSeverity | str | None = None
    element_internal_id: Optional[str] = None
    element_ifc_guid: Optional[str] = None
    element_type: Optional[str] = None
    model_name: Optional[str] = None
    model_fingerprint: str = ""
    storey_id: Optional[str] = None
    requirement: Optional[str] = None
    clause_id: Optional[str] = None
    clause_text: Optional[str] = None
    source: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, Verdict):
            self.verdict = Verdict(str(self.verdict))
        if isinstance(self.category, FindingStage):
            stage = self.category
        else:
            try:
                stage = FindingStage(str(self.category))
            except ValueError:
                stage = FindingStage.COMPLIANCE
        self.category = stage.value

        if self.element_internal_id is None:
            self.element_internal_id = self.element_id
        if self.element_id is None:
            self.element_id = self.element_internal_id
        if self.element_type is None:
            self.element_type = self.object
        if self.clause_id is None and stage is FindingStage.COMPLIANCE:
            self.clause_id = self.article_id
        if self.clause_text is None:
            self.clause_text = self.rule_text_en
        if self.requirement is None:
            self.requirement = self.clause_text

        if self.severity is None:
            self.severity = self._default_severity(stage)
        elif not isinstance(self.severity, FindingSeverity):
            self.severity = FindingSeverity(str(self.severity).lower())

    def _default_severity(self, stage: FindingStage) -> FindingSeverity:
        if stage is FindingStage.SCHEMA:
            return FindingSeverity.FAIL if self.verdict == Verdict.FAIL else FindingSeverity.ALERT
        if self.verdict == Verdict.FAIL:
            return FindingSeverity.FAIL
        if self.verdict in (Verdict.NEEDS_REVIEW, Verdict.NOT_EVALUATED):
            return FindingSeverity.ALERT
        return FindingSeverity.INFO

    @property
    def stage(self) -> FindingStage:
        return FindingStage(self.category)

    @property
    def element_key(self) -> str:
        return self.element_ifc_guid or self.element_internal_id or self.element_id or ""

    @property
    def _id_basis(self) -> str:
        code = self.code or ""
        clause = self.clause_id or (self.article_id if self.stage is FindingStage.COMPLIANCE else "")
        parts = [
            "v1",
            self.stage.value,
            code,
            self.model_fingerprint or "",
            self.element_key,
            clause or "",
        ]
        basis = _US.join(parts)
        # Existing Stage-8 agents may legitimately emit more than one finding
        # with the same semantic basis. The deterministic ordinal assigned by
        # assign_finding_ordinals is the migration-safe disambiguator.
        if self.ordinal:
            basis += _US + str(self.ordinal)
        return basis

    @property
    def finding_id(self) -> str:
        return str(uuid.uuid5(SIMSYS_FINDING_NAMESPACE, self._id_basis))

    def to_dict(self) -> dict[str, Any]:
        expected = self.expected if self.expected is not None else self.required
        actual = self.actual if self.actual is not None else self.measured
        return {
            # Stable shared fields
            "finding_id": self.finding_id,
            "stage": self.stage.value,
            "category": self.stage.value,
            "code": self.code,
            "severity": _enum_value(self.severity),
            "verdict": self.verdict.value,
            "message": self.message,
            "element_internal_id": self.element_internal_id,
            "element_ifc_guid": self.element_ifc_guid,
            "element_type": self.element_type,
            "model_name": self.model_name,
            "model_fingerprint": self.model_fingerprint or None,
            "storey_id": self.storey_id,
            "requirement": self.requirement,
            "expected": expected,
            "actual": actual,
            "unit": self.unit,
            "clause_id": self.clause_id,
            "clause_text": self.clause_text,
            "source": self.source,
            "details": dict(self.details),
            "unsupported": self.unsupported,
            # Backward-compatible Stage-8 keys
            "article_id": self.article_id,
            "object": self.object,
            "measured": self.measured,
            "required": self.required,
            "element_id": self.element_id,
            "rule_text_en": self.rule_text_en,
        }

    @classmethod
    def schema(
        cls,
        *,
        code: str,
        severity: FindingSeverity | str,
        message: str,
        entity: Optional[str] = None,
        guid: Optional[str] = None,
        model_name: Optional[str] = None,
        model_fingerprint: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> "Finding":
        sev = severity if isinstance(severity, FindingSeverity) else FindingSeverity(str(severity).lower())
        verdict = Verdict.FAIL if sev is FindingSeverity.FAIL else Verdict.NOT_EVALUATED
        payload = dict(details or {})
        if entity is not None:
            payload.setdefault("entity", entity)
        if guid is not None:
            payload.setdefault("guid", guid)
        return cls(
            article_id=code,
            verdict=verdict,
            message=message,
            object=entity,
            element_ifc_guid=guid,
            element_type=entity,
            category=FindingStage.SCHEMA.value,
            code=code,
            severity=sev,
            model_name=model_name,
            model_fingerprint=model_fingerprint,
            details=payload,
        )
