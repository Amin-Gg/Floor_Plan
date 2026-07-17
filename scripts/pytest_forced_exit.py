#!/usr/bin/env python3
"""Run pytest and force process termination after report finalization.

Some optional engine libraries start non-daemon cleanup threads in this audit
runtime. Pytest finishes and writes its JUnit report, but Python can remain alive.
This wrapper preserves pytest's exit code and forces a clean process boundary.
"""
from __future__ import annotations

import os
import sys

import pytest


if __name__ == "__main__":
    code = int(pytest.main(sys.argv[1:]))
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)
