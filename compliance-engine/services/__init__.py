"""
services — the deterministic compliance engine (spatial graph, the four agents,
orchestrator, report generator, review queue).

The engine modules import one another by bare module name (e.g.
``from numeric_checker import Verdict``), a flat-layout convention kept from the
original codebase. To let ``from services.orchestrator import run_compliance``
work from a plain checkout (no PYTHONPATH needed), this package adds its own
directory to ``sys.path`` on import. Under pytest the same is achieved via
``[tool.pytest.ini_options] pythonpath`` in pyproject.toml.
"""
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)
