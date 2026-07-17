"""Protocol implemented by every model-quality check plugin.

Plugins are intentionally small and side-effect free. They inspect the canonical
:class:`domain.model.BuildingModel` and return shared-domain ``Finding``
objects. They must not execute compliance rules, generate reports, mutate global
state, or swallow exceptions.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.findings import Finding
from domain.model import BuildingModel

from .context import QualityContext


@runtime_checkable
class QualityCheck(Protocol):
    """A single independently testable quality-check plugin."""

    code_prefix: str
    codes: tuple[str, ...]
    name: str
    blocking: bool

    def applies_to(self, model: BuildingModel, context: QualityContext) -> bool:
        """Return whether this plugin is relevant to the current model."""
        ...

    def run(self, model: BuildingModel, context: QualityContext) -> list[Finding]:
        """Return findings without mutating ``model`` or global state."""
        ...
