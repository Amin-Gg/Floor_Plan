#!/usr/bin/env python3
"""Run the final release acceptance suite and write machine-readable evidence.

The default mode runs the complete regression matrix. ``--static-only`` is
intended for review hosts that do not have the pinned Python 3.11/IfcOpenShell,
Flask, CUDA, model weights, Redis, or Docker runtime. Static-only mode never
claims that the full runtime regression was re-executed; it records the latest
verified summaries separately.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def run(
    name: str,
    command: list[str],
    out_dir: Path,
    timeout: int = 1800,
    *,
    allow_environment_blocker: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    (out_dir / f"{name}.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (out_dir / f"{name}.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    blocked = allow_environment_blocker and completed.returncode == 2
    return {
        "name": name,
        "command": command,
        "passed": completed.returncode == 0,
        "status": "blocked_environment" if blocked else ("passed" if completed.returncode == 0 else "failed"),
        "required": not blocked,
        "returncode": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def validate_serialized_files() -> dict[str, Any]:
    errors: list[str] = []
    json_files = 0
    yaml_files = 0
    for path in sorted(ROOT.rglob("*.json")):
        if any(part in {".git", ".venv", "release"} for part in path.parts):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
            json_files += 1
        except Exception as exc:  # noqa: BLE001 - evidence should include exact file
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(ROOT.rglob(pattern)):
            if any(part in {".git", ".venv", "release"} for part in path.parts):
                continue
            try:
                yaml.safe_load(path.read_text(encoding="utf-8"))
                yaml_files += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.relative_to(ROOT)}: {exc}")
    return {
        "name": "serialized-contracts",
        "passed": not errors,
        "json_files": json_files,
        "yaml_files": yaml_files,
        "errors": errors,
    }


def validate_markdown_links() -> dict[str, Any]:
    errors: list[str] = []
    checked = 0
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in sorted(ROOT.rglob("*.md")):
        if any(part in {".git", ".venv", "release"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for raw in pattern.findall(text):
            target = raw.split("#", 1)[0].strip()
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            checked += 1
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"{path.relative_to(ROOT)} -> outside repository: {target}")
                continue
            if not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)} -> missing: {target}")
    return {"name": "markdown-links", "passed": not errors, "checked": checked, "errors": errors}


def previous_verified_evidence() -> dict[str, Any]:
    base = ROOT / "release" / "evidence" / "phase8"
    result: dict[str, Any] = {"source": "verified Phase 8 evidence retained during final cleanup"}
    for name in ("stage1_test_summary", "engine_test_summary", "evaluation_acceptance", "empirical_status"):
        path = base / f"{name}.json"
        if path.is_file():
            result[name] = json.loads(path.read_text(encoding="utf-8"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "release" / "local-acceptance")
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    checks: list[dict[str, Any]] = [validate_serialized_files(), validate_markdown_links()]
    checks.extend([
        run("phase9-cleanup-tests", [sys.executable, "-m", "pytest", "-q", "tests/test_phase9_final_release.py"], out, 120),
        run("compileall", [sys.executable, "-m", "compileall", "-q", "application.py", "config", "routes", "services", "models", "evaluation", "export", "validation", "scripts", "compliance-engine"], out, 300),
        run("dependency-locks", [sys.executable, "scripts/verify_dependency_locks.py"], out, 180),
        run("container-contracts", [sys.executable, "scripts/validate_container_contracts.py"], out, 180),
        run("security-contracts", [sys.executable, "scripts/validate_security_contracts.py"], out, 180),
        run(
            "ruff-baseline",
            [sys.executable, "scripts/check_ruff_baseline.py"],
            out,
            180,
            allow_environment_blocker=args.static_only,
        ),
        run("sbom", [sys.executable, "scripts/generate_sbom.py"], out, 300),
    ])

    if not args.static_only:
        checks.extend([
            run("openapi", [sys.executable, "scripts/generate_openapi.py", "--check"], out, 180),
            run("stage1-tests", [sys.executable, "scripts/run_stage1_test_matrix.py", str(out / "stage1-tests")], out, 7200),
            run("engine-tests", [sys.executable, "scripts/run_engine_test_matrix.py", str(out / "engine-tests")], out, 7200),
            run("trust-boundary", [sys.executable, "scripts/run_phase3_acceptance.py", "--out", str(out / "trust-boundary")], out, 1800),
            run("detector-semantics", [sys.executable, "scripts/run_phase4_acceptance.py", "--out", str(out / "detector-semantics")], out, 1800),
            run("reproducibility", [sys.executable, "scripts/run_phase6_acceptance.py", "--out", str(out / "reproducibility.json")], out, 1800),
            run("security-http", [sys.executable, "scripts/run_phase7_acceptance.py", "--out", str(out / "security-http")], out, 1800),
            run("ml-evaluation", [sys.executable, "scripts/run_phase8_acceptance.py", "--out", str(out / "ml-evaluation")], out, 1800),
        ])

    payload = {
        "schema_version": "floorplan-final-acceptance-v1",
        "mode": "static-only" if args.static_only else "full",
        "passed": all(
            check.get("passed") is True
            for check in checks
            if check.get("required", True)
        ),
        "checks": checks,
        "previous_verified_evidence": previous_verified_evidence(),
        "limitations": (
            [
                "Full runtime regression was not re-executed in static-only mode.",
                *[
                    f"{check['name']} was blocked by the review-host environment."
                    for check in checks
                    if check.get("status") == "blocked_environment"
                ],
            ]
            if args.static_only else []
        ),
    }
    (out / "acceptance_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
