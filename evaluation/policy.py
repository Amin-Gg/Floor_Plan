"""Release-policy evaluation separate from raw metric calculation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_policy(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema_version") != "1.0":
        raise ValueError("Unsupported Phase-8 evaluation policy")
    return value


def apply_policy(report: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, actual: Any, expected: Any, blocker: bool = True) -> None:
        checks.append({"name": name, "passed": bool(passed), "actual": actual, "expected": expected, "blocker": blocker})

    minimum = policy["minimum_dataset"]
    record("human_verified_labels", report.get("empirical_claims_allowed") is True, report.get("empirical_claims_allowed"), True)
    record("minimum_samples", report["summary"]["samples"] >= minimum["samples"], report["summary"]["samples"], minimum["samples"])
    record("minimum_instances", report["summary"]["instances"] >= minimum["instances"], report["summary"]["instances"], minimum["instances"])
    for class_name, required in minimum.get("instances_per_class", {}).items():
        actual = (report["classes"].get(class_name) or {}).get("support", 0)
        record(f"support:{class_name}", actual >= required, actual, required)

    thresholds = policy["thresholds"]
    for class_name, expected in thresholds.get("classes", {}).items():
        actual = report["classes"].get(class_name)
        if not actual:
            record(f"class_present:{class_name}", False, None, "present")
            continue
        for metric, minimum_value in expected.items():
            value = actual.get(metric)
            record(f"{class_name}:{metric}", isinstance(value, (int, float)) and value >= minimum_value, value, f">={minimum_value}")
    maximum_ece = thresholds.get("maximum_ece")
    if maximum_ece is not None:
        value = report["calibration_overall"].get("ece")
        record("maximum_ece", value is not None and value <= maximum_ece, value, f"<={maximum_ece}")
    maximum_scale_error = thresholds.get("maximum_scale_relative_error_mean")
    if maximum_scale_error is not None:
        value = report["scale"]["relative_error"].get("mean")
        record("maximum_scale_relative_error_mean", value is not None and value <= maximum_scale_error, value, f"<={maximum_scale_error}")
    record("critical_false_pass", report["verdict_impact"]["critical_false_pass"] == 0, report["verdict_impact"]["critical_false_pass"], 0)
    minimum_verdict = thresholds.get("minimum_verdict_exact_agreement")
    if minimum_verdict is not None:
        value = report["verdict_impact"]["exact_agreement"]
        record("minimum_verdict_exact_agreement", value >= minimum_verdict, value, f">={minimum_verdict}")
    passed = all(row["passed"] for row in checks if row["blocker"])
    return {"schema_version": "1.0", "passed": passed, "checks": checks, "policy_id": policy.get("policy_id")}
