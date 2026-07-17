#!/usr/bin/env python3
"""Static Phase-7 production security contract validation."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def numeric_default(value) -> int:
    match = re.search(r":-(\d+)\}", str(value))
    return int(match.group(1)) if match else int(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    checks = []
    checks.append(check("engine-not-public", "ports" not in services["compliance-api"], "engine is backend-only"))
    checks.append(check("backend-network-internal", compose["networks"]["backend"].get("internal") is True, "backend network is internal"))
    for name in ("redis", "compliance-api", "compliance-worker", "floorplan-api"):
        service = services[name]
        checks.extend([
            check(f"{name}:read-only", service.get("read_only") is True, "root filesystem read-only"),
            check(f"{name}:cap-drop", "ALL" in service.get("cap_drop", []), "all Linux capabilities dropped"),
            check(f"{name}:no-new-privileges", "no-new-privileges:true" in service.get("security_opt", []), "privilege escalation blocked"),
            check(f"{name}:memory-limit", bool(service.get("mem_limit")), str(service.get("mem_limit"))),
            check(f"{name}:cpu-limit", bool(service.get("cpus")), str(service.get("cpus"))),
            check(f"{name}:pid-limit", int(service.get("pids_limit", 0)) > 0, str(service.get("pids_limit"))),
        ])
    floor_env = services["floorplan-api"]["environment"]
    engine_env = services["compliance-api"]["environment"]
    checks.extend([
        check("stage1:api-key-secret", floor_env.get("FLOORPLAN_API_KEYS_FILE") == "/run/secrets/floorplan_api_keys", "Docker secret"),
        check("stage1:process-isolation", floor_env.get("INFERENCE_ISOLATION") == "process", "hard process boundary"),
        check("stage1:readiness", services["floorplan-api"]["healthcheck"]["test"][-1].endswith("/readyz"), "readiness probe"),
        check("engine:api-key-secret", engine_env.get("COMPLIANCE_API_KEYS_FILE") == "/run/secrets/compliance_api_key", "Docker secret"),
        check("engine:strict-job-store", engine_env.get("STRICT_JOB_STORE") == "1", "Redis fallback disabled"),
        check("engine:hard-time-limit", numeric_default(engine_env.get("COMPLIANCE_JOB_HARD_TIME_LIMIT", 0)) > 0, str(engine_env.get("COMPLIANCE_JOB_HARD_TIME_LIMIT"))),
        check("worker:child-recycling", any("max-tasks-per-child" in str(x) for x in services["compliance-worker"]["command"]), "worker child recycling"),
        check("secret-values-absent", not any((ROOT / "secrets").glob("*.txt")), "no committed runtime secret values"),
    ])
    stage_docker = (ROOT / "Dockerfile").read_text()
    engine_docker = (ROOT / "compliance-engine" / "Dockerfile").read_text()
    checks.extend([
        check("stage1:readiness-healthcheck", "/readyz" in stage_docker, "Dockerfile probe"),
        check("engine:readiness-healthcheck", "/readyz" in engine_docker, "Dockerfile probe"),
        check("stage1:non-root", bool(re.search(r"^USER app$", stage_docker, re.M)), "non-root runtime"),
        check("engine:non-root", bool(re.search(r"^USER app$", engine_docker, re.M)), "non-root runtime"),
    ])
    payload = {
        "schema_version": "phase7-production-security-contract-v1",
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
