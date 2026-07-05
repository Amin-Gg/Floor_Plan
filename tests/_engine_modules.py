"""
tests/_engine_modules.py
========================
Single source of truth for the Step-1 ↔ Step-2 interface logic.

The engine's own ``compliance-engine/ingest/`` package IS the production
IFC loader and review pre-pass. Section 1 used to keep private copies in
``interface/`` for its round-trip tests; those copies drifted (they lacked
the scale-confidence gate, the contract scale/building-params Pset reading
and ``downgrade_flagged_findings``), so green Section-1 tests were
validating an older contract than the engine actually enforces. The copies
were removed — this module loads the production files directly so the
round-trip tests exercise exactly what runs in Step 2.

Loading is by file path under collision-free aliases, NOT by inserting the
engine tree into ``sys.path``: the engine also contains ``services/``,
``tests/``, ``api/`` … whose names collide with Section 1's own packages.
Both modules are import-time standalone (stdlib-only tops; ifcopenshell and
the flat agent imports inside them are lazy), so file loading is safe.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ENGINE_INGEST = (
    Path(__file__).resolve().parents[1] / "compliance-engine" / "ingest"
)


def _load(filename: str, alias: str):
    """Load compliance-engine/ingest/<filename> as module ``alias`` (cached)."""
    if alias in sys.modules:
        return sys.modules[alias]
    path = _ENGINE_INGEST / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:  # pragma: no cover — repo layout broken
        raise ImportError(f"cannot load engine module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


_ifc_mod = _load("ifc_to_bim_data.py", "engine_ingest_ifc_to_bim_data")
_rp_mod = _load("review_prepass.py", "engine_ingest_review_prepass")

# Re-export the production API under the names the tests use.
ifc_to_bim_data = _ifc_mod.ifc_to_bim_data
apply_review_prepass = _rp_mod.apply_review_prepass
downgrade_flagged_findings = _rp_mod.downgrade_flagged_findings
