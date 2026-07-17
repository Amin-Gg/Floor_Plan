#!/usr/bin/env python3
"""Generate or verify deterministic current OpenAPI snapshots for both public APIs."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STAGE1_PATH = ROOT / "contracts" / "openapi_stage1.json"
ENGINE_PATH = ROOT / "compliance-engine" / "docs" / "contracts" / "openapi.json"


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def stage1_spec() -> dict:
    os.environ.setdefault("APP_ENV", "testing")
    os.environ.setdefault("FLOORPLAN_SKIP_MODEL_INIT", "1")
    from application import create_app
    from config.settings import TestingConfig
    app = create_app(TestingConfig)
    response = app.test_client().get("/openapi/openapi.json")
    if response.status_code != 200:
        raise RuntimeError(f"Stage 1 OpenAPI returned {response.status_code}")
    return response.get_json()


def engine_spec() -> dict:
    code = (
        "import json; from api.main import app; "
        "print(json.dumps(app.openapi(), ensure_ascii=False, sort_keys=True))"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "compliance-engine"), "ALLOW_EMPTY_CLAUSES": "1"}
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT / "compliance-engine",
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def write_or_check(path: Path, text: str, check: bool) -> bool:
    if check:
        return path.is_file() and path.read_text(encoding="utf-8") == text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    values = {
        STAGE1_PATH: canonical(stage1_spec()),
        ENGINE_PATH: canonical(engine_spec()),
    }
    mismatches = [str(path.relative_to(ROOT)) for path, text in values.items() if not write_or_check(path, text, args.check)]
    print(json.dumps({"check": args.check, "mismatches": mismatches, "paths": [str(p.relative_to(ROOT)) for p in values]}, sort_keys=True))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
