#!/usr/bin/env python3
"""Run compliance-engine test modules in isolated process groups."""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "compliance-engine"


@dataclass
class Result:
    name: str
    test_file: str
    tests: int
    failures: int
    errors: int
    skipped: int
    returncode: int | None
    timed_out: bool
    cleanup_forced: bool
    junit_complete: bool
    passed: bool
    duration_seconds: float


def junit(path: Path) -> tuple[bool, dict[str, int]]:
    values = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    if not path.is_file() or path.stat().st_size == 0:
        return False, values
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        if not suites:
            return False, values
        for suite in suites:
            for key in values:
                values[key] += int(float(suite.attrib.get(key, "0")))
        return True, values
    except (OSError, ET.ParseError, ValueError):
        return False, values


def stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_one(test_file: Path, out: Path, timeout: int) -> Result:
    name = "__".join(test_file.relative_to(ENGINE).with_suffix("").parts)
    junit_path = out / f"{name}.junit.xml"
    stdout_path = out / f"{name}.stdout.txt"
    stderr_path = out / f"{name}.stderr.txt"
    command = [sys.executable, str(ROOT / "scripts" / "pytest_forced_exit.py"), "-q", str(test_file), f"--junitxml={junit_path}"]
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "ALLOW_EMPTY_CLAUSES": "1", "PYTHONPATH": str(ENGINE)}
    started = time.monotonic()
    timed_out = cleanup_forced = False
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(command, cwd=ENGINE, env=env, stdout=stdout, stderr=stderr, text=True, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            complete, counts = junit(junit_path)
            cleanup_forced = complete and counts["failures"] == 0 and counts["errors"] == 0
            stop(proc)
    complete, counts = junit(junit_path)
    passed = complete and counts["tests"] > 0 and counts["failures"] == 0 and counts["errors"] == 0
    if not cleanup_forced:
        passed = passed and proc.returncode == 0 and not timed_out
    return Result(
        name=name,
        test_file=str(test_file.relative_to(ENGINE)),
        returncode=proc.returncode,
        timed_out=timed_out,
        cleanup_forced=cleanup_forced,
        junit_complete=complete,
        passed=passed,
        duration_seconds=round(time.monotonic() - started, 3),
        **counts,
    )



def contains_pytest_tests(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return True
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        for node in ast.walk(tree)
    )

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default="release/local/engine-shards")
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    for path in out.iterdir():
        if path.is_file():
            path.unlink()
    all_files = sorted((ENGINE / "tests").rglob("test_*.py")) + sorted((ENGINE / "eval").rglob("test_*.py"))
    files = [path for path in all_files if contains_pytest_tests(path)]
    excluded = [str(path.relative_to(ENGINE)) for path in all_files if path not in files]
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_one, path, out, args.timeout): path for path in files}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[engine-shard] {'PASS' if result.passed else 'FAIL'} {result.name} ({result.tests} tests, {result.duration_seconds}s)", flush=True)
    results.sort(key=lambda row: row.name)
    totals = {key: sum(getattr(row, key) for row in results) for key in ("tests", "failures", "errors", "skipped")}
    payload = {
        "schema_version": "phase8-engine-process-matrix-v1",
        "passed": bool(results) and all(row.passed for row in results),
        "files": len(files),
        "excluded_script_style_files": excluded,
        "workers": args.workers,
        "timeout_seconds": args.timeout,
        "totals": totals,
        "cleanup_forced_shards": [row.name for row in results if row.cleanup_forced],
        "shards": [asdict(row) for row in results],
    }
    (out / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": payload["passed"], "files": len(files), "totals": totals}, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
