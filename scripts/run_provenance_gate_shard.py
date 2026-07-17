#!/usr/bin/env python3
"""Execute the cleanup-sensitive provenance tests with a hard process boundary.

The test functions remain the source of truth. This runner only replaces
pytest's session cleanup, which can hang after IfcOpenShell/native subprocess use
on some hosts. It emits ordinary JUnit XML and exits with os._exit.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

BRIDGE_TESTS = (
    "test_needs_review_flag_propagates",
    "test_low_confidence_flagged",
    "test_high_confidence_not_flagged",
    "test_threshold_is_configurable",
    "test_missing_provenance_defaults_to_confident",
)
EXPORT_TESTS = ("test_end_to_end_untyped_room_flagged",)


def write_junit(path: Path, cases: list[dict[str, object]], elapsed: float) -> None:
    failures = sum(1 for case in cases if case.get("failure"))
    suite = ET.Element(
        "testsuite",
        {
            "name": "pytest",
            "tests": str(len(cases)),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
            "time": f"{elapsed:.6f}",
        },
    )
    for case in cases:
        node = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "tests.test_provenance_gate",
                "name": str(case["name"]),
                "time": f"{float(case['time']):.6f}",
            },
        )
        if case.get("failure"):
            failure = ET.SubElement(node, "failure", {"message": str(case["failure"])[0:500]})
            failure.text = str(case["failure"])
    root = ET.Element("testsuites", {"name": "pytest tests"})
    root.append(suite)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("bridge", "export"), required=True)
    parser.add_argument("--junit", type=Path, required=True)
    args = parser.parse_args()

    import test_provenance_gate as module

    names = BRIDGE_TESTS if args.mode == "bridge" else EXPORT_TESTS
    cases: list[dict[str, object]] = []
    started = time.monotonic()
    for name in names:
        test_started = time.monotonic()
        failure = ""
        try:
            function = getattr(module, name)
            if name == "test_end_to_end_untyped_room_flagged":
                with tempfile.TemporaryDirectory(prefix="phase5-provenance-") as folder:
                    function(Path(folder))
            else:
                function()
        except BaseException:  # noqa: BLE001 - test runner must serialize every failure
            failure = traceback.format_exc()
        cases.append(
            {
                "name": name,
                "time": time.monotonic() - test_started,
                "failure": failure,
            }
        )
    write_junit(args.junit, cases, time.monotonic() - started)
    passed = not any(case["failure"] for case in cases)
    print("." * len(cases) + " " * 8 + "[100%]", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    code = main()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)
