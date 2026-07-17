#!/usr/bin/env python3
"""Fail only when Ruff introduces findings beyond the frozen Phase-0 debt.

Known findings may disappear in later phases; disappearance passes. Any new
finding, including a known code at a different location, fails and must either
be fixed or deliberately re-baselined with an explanation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "ruff-baseline.json"


def run_ruff(cwd: Path, target: str = ".") -> list[dict]:
    proc = subprocess.run(
        ["ruff", "check", target, "--output-format=json"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        raise RuntimeError(f"ruff failed (exit={proc.returncode}): {proc.stderr}")
    return json.loads(proc.stdout or "[]")


def normalized(items: list[dict], base: Path) -> set[tuple]:
    rows = set()
    for item in items:
        path = Path(item["filename"]).resolve()
        try:
            rel = path.relative_to(base.resolve()).as_posix()
        except ValueError:
            rel = path.name
        rows.add((
            rel,
            item["code"],
            int(item["location"]["row"]),
            int(item["location"]["column"]),
            item["message"],
        ))
    return rows


def baseline_set(items: list[dict]) -> set[tuple]:
    return {(x["path"], x["code"], int(x["row"]), int(x["column"]), x["message"]) for x in items}


def main() -> int:
    if shutil.which("ruff") is None:
        print(json.dumps({
            "status": "blocked_environment",
            "reason": "ruff executable is not installed on this host",
        }, indent=2))
        return 2
    frozen = json.loads(BASELINE.read_text(encoding="utf-8"))
    current_stage1 = normalized(run_ruff(ROOT), ROOT)
    current_engine = normalized(run_ruff(ROOT / "compliance-engine"), ROOT / "compliance-engine")
    new_stage1 = sorted(current_stage1 - baseline_set(frozen["stage1"]))
    new_engine = sorted(current_engine - baseline_set(frozen["engine"]))
    payload = {
        "stage1_current": len(current_stage1),
        "stage1_baseline": len(frozen["stage1"]),
        "engine_current": len(current_engine),
        "engine_baseline": len(frozen["engine"]),
        "new_stage1": new_stage1,
        "new_engine": new_engine,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if new_stage1 or new_engine else 0


if __name__ == "__main__":
    raise SystemExit(main())
