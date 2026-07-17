#!/usr/bin/env python3
"""Run Stage-1 test files in isolated process groups and aggregate JUnit evidence.

Each test module gets a fresh native-library process boundary. A completed JUnit
report is authoritative if the host hangs only during native cleanup; the process
group is then terminated and the forced cleanup is recorded in summary.json.
"""
from __future__ import annotations

import argparse
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


@dataclass
class Result:
    name: str
    command: list[str]
    returncode: int | None
    timed_out: bool
    cleanup_forced: bool
    junit_complete: bool
    tests: int
    failures: int
    errors: int
    skipped: int
    passed: bool
    duration_seconds: float


def junit_counts(path: Path) -> tuple[bool, dict[str, int]]:
    empty = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    if not path.exists() or path.stat().st_size == 0:
        return False, empty
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        if not suites:
            return False, empty
        totals = empty.copy()
        for suite in suites:
            for key in totals:
                totals[key] += int(float(suite.attrib.get(key, "0")))
        return True, totals
    except (ET.ParseError, OSError, ValueError):
        return False, empty


def terminate_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run_one(name: str, command: list[str], junit: Path, out_dir: Path, timeout: int) -> Result:
    stdout_path = out_dir / f"{name}.stdout.txt"
    stderr_path = out_dir / f"{name}.stderr.txt"
    env = os.environ.copy()
    env.update({
        "APP_ENV": "testing",
        "FLOORPLAN_SKIP_MODEL_INIT": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    started = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    timed_out = False
    cleanup_forced = False
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            complete, counts = junit_counts(junit)
            if complete and counts["failures"] == 0 and counts["errors"] == 0:
                cleanup_forced = True
            terminate_group(proc)
    complete, counts = junit_counts(junit)
    returncode = proc.returncode if proc else None
    passed = complete and counts["tests"] > 0 and counts["failures"] == 0 and counts["errors"] == 0
    if not cleanup_forced:
        passed = passed and returncode == 0 and not timed_out
    return Result(
        name=name,
        command=command,
        returncode=returncode,
        timed_out=timed_out,
        cleanup_forced=cleanup_forced,
        junit_complete=complete,
        passed=passed,
        duration_seconds=round(time.monotonic() - started, 3),
        **counts,
    )


def jobs(out_dir: Path) -> list[tuple[str, list[str], Path]]:
    result: list[tuple[str, list[str], Path]] = []
    for test_file in sorted((ROOT / "tests").glob("test_*.py")):
        stem = test_file.stem
        if stem == "test_provenance_gate":
            for mode in ("bridge", "export"):
                name = f"{stem}_{mode}"
                junit = out_dir / f"{name}.junit.xml"
                command = [sys.executable, str(ROOT / "scripts/run_provenance_gate_shard.py"), "--mode", mode, "--junit", str(junit)]
                result.append((name, command, junit))
        else:
            junit = out_dir / f"{stem}.junit.xml"
            command = [sys.executable, str(ROOT / "scripts/pytest_forced_exit.py"), "-q", str(test_file), f"--junitxml={junit}"]
            result.append((stem, command, junit))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", nargs="?", default="release/local/stage1-shards")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.junit.xml", "*.stdout.txt", "*.stderr.txt", "summary.json"):
        for path in out_dir.glob(pattern):
            path.unlink()

    work = jobs(out_dir)
    # IfcOpenShell's provenance bridge is stable in an isolated process but may
    # contend with simultaneous native initialisation. Run provenance shards
    # serially after the ordinary modules; all other modules remain parallel.
    parallel_work = [item for item in work if not item[0].startswith("test_provenance_gate_")]
    serial_work = [item for item in work if item[0].startswith("test_provenance_gate_")]
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_one, name, command, junit, out_dir, args.timeout): name for name, command, junit in parallel_work}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[stage1-shard] {'PASS' if result.passed else 'FAIL'} {result.name} ({result.tests} tests, {result.duration_seconds}s)", flush=True)
    for name, command, junit in serial_work:
        result = run_one(name, command, junit, out_dir, args.timeout)
        results.append(result)
        print(f"[stage1-shard] {'PASS' if result.passed else 'FAIL'} {result.name} ({result.tests} tests, {result.duration_seconds}s)", flush=True)

    results.sort(key=lambda item: item.name)
    totals = {key: sum(getattr(item, key) for item in results) for key in ("tests", "failures", "errors", "skipped")}
    payload = {
        "schema_version": "phase6-stage1-process-matrix-v1",
        "runner": "parallel-process-groups-junit-authoritative-on-native-cleanup-only",
        "workers": args.workers,
        "timeout_seconds": args.timeout,
        "passed": bool(results) and all(item.passed for item in results),
        "totals": totals,
        "cleanup_forced_shards": [item.name for item in results if item.cleanup_forced],
        "shards": [asdict(item) for item in results],
    }
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": payload["passed"], "totals": totals, "cleanup_forced_shards": payload["cleanup_forced_shards"]}, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
