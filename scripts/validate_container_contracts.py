#!/usr/bin/env python3
"""Static, daemon-free validation of Dockerfiles, build contexts and Compose contracts."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(ok), "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    root = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    engine = (ROOT / "compliance-engine/Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    engine_ignore = (ROOT / "compliance-engine/Dockerfile.dockerignore").read_text(encoding="utf-8")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    images = json.loads((ROOT / "containers-base-images.lock.json").read_text(encoding="utf-8"))
    checks: list[dict] = []

    for label, text in (("stage1", root), ("engine", engine)):
        checks.extend(
            [
                check(f"{label}:hashed-runtime-lock", "--require-hashes" in text and "runtime.lock" in text, "pip hash enforcement"),
                check(f"{label}:external-torch", "pip install --no-deps" in text and "torch-2.1.2+cu118" in text, "sealed local torch wheel"),
                check(f"{label}:pip-check", "python -m pip check" in text, "dependency consistency gate"),
                check(f"{label}:non-root", "USER app" in text, "runtime user"),
                check(f"{label}:healthcheck", "HEALTHCHECK" in text, "container health probe"),
                check(f"{label}:oci-labels", "org.opencontainers.image.version" in text, "build provenance labels"),
            ]
        )

    python_source = images["python_source"]
    checks.extend(
        [
            check("stage1:python-source-version", f"ARG PYTHON_VERSION={python_source['version']}" in root, python_source["version"]),
            check("stage1:python-source-hash", python_source["sha256"] in root and "sha256sum --check --strict" in root, python_source["sha256"]),
            check("stage1:no-unavailable-ubuntu-python-package", "apt-get install" in root and "python3.11 python3.11-venv" not in root, "CPython is built from sealed source"),
            check("stage1:verifier-in-build-context", "scripts/*" in dockerignore and "!scripts/verify_external_artifacts.py" in dockerignore, "artifact verifier is explicitly included"),
            check("engine:dockerfile-ignore", all(token in engine_ignore for token in ("!compliance-engine/**", "!wheels/torch-2.1.2", "!scripts/verify_external_artifacts.py")), "engine root-context allow-list"),
            check("engine:no-build-time-model-download", "SentenceTransformer(" not in engine and "huggingface_hub" not in engine, "models are external/offline"),
            check("engine:current-python-311", images["images"]["python_311"] in engine, images["images"]["python_311"]),
        ]
    )

    services = compose.get("services", {})
    required = {"redis", "floorplan-api", "compliance-api", "compliance-worker"}
    checks.extend(
        [
            check("compose:services", required <= set(services), ",".join(sorted(services))),
            check("compose:linux-amd64", all(services[name].get("platform") == "linux/amd64" for name in required), "all services are platform-pinned"),
            check("compose:no-latest", all(":latest" not in str(value.get("image", "")) for value in services.values()), "immutable version tags used"),
            check("compose:no-wildcard-cors", services["floorplan-api"]["environment"]["APP_CORS_ORIGINS"] != "*", "production default is explicit"),
            check("compose:engine-root-context", services["compliance-api"]["build"]["context"] == "." and services["compliance-api"]["build"]["dockerfile"] == "compliance-engine/Dockerfile", "shared sealed wheel context"),
            check("compose:profiles", set(services["floorplan-api"]["profiles"]) == {"floorplan-only", "full-pipeline"} and "full-pipeline" in services["compliance-api"]["profiles"], "execution profiles"),
            check("compose:redis-pinned", re.fullmatch(r"redis:\d+\.\d+\.\d+-alpine@sha256:[0-9a-f]{64}", services["redis"]["image"]) is not None, services["redis"]["image"]),
        ]
    )

    payload = {"schema_version": "phase6-container-contract-v2", "passed": all(item["passed"] for item in checks), "checks": checks}
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.json_out:
        target = args.json_out if args.json_out.is_absolute() else ROOT / args.json_out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
