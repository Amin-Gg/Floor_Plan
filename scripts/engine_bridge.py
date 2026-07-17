#!/usr/bin/env python3
"""Compatibility launcher for the engine's public JSON CLI.

The process is replaced with ``python -m api.public_cli`` rather than wrapping
it in another subprocess. This preserves the public boundary and guarantees
that the CLI's hard exit terminates the exact process observed by callers.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "compliance-engine"


def main() -> None:
    if not (ENGINE_ROOT / "api" / "public_cli.py").is_file():
        raise FileNotFoundError("compliance-engine public CLI is missing")
    os.chdir(ENGINE_ROOT)
    env = {**os.environ, "PYTHONPATH": str(ENGINE_ROOT)}
    command = [sys.executable, "-m", "api.public_cli", *sys.argv[1:]]
    os.execvpe(sys.executable, command, env)


if __name__ == "__main__":
    main()
