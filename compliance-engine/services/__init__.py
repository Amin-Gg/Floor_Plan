"""Deterministic engine implementation modules.

Public requests must enter through ``services.validation_pipeline``.  Agent
modules remain implementation details and may use the historical flat-import
layout while migration continues.
"""
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)
