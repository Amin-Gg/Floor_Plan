#!/usr/bin/env python3
"""Reproducible workspace/artifact/environment preflight for the final unified release.

Examples:
  python scripts/preflight.py --mode code
  python scripts/preflight.py --mode floorplan-only --strict
  python scripts/preflight.py --mode full-pipeline --refresh-manifest --strict
  python scripts/preflight.py --mode full-pipeline --json-out release/local/preflight.json
"""
from __future__ import annotations

import argparse
import hashlib
import email
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verify_external_artifacts import tree_digest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "artifacts-manifest.json"
EXPECTED_WHEELS = {
    "torch-cu118": {
        "filename": re.compile(r"^torch-2\.1\.2\+cu118-cp311-cp311-linux_x86_64\.whl$"),
        "distribution": "torch",
        "version": "2.1.2+cu118",
        "tag": "cp311-cp311-linux_x86_64",
    },
    "torchvision-cu118": {
        "filename": re.compile(r"^torchvision-0\.16\.2\+cu118-cp311-cp311-linux_x86_64\.whl$"),
        "distribution": "torchvision",
        "version": "0.16.2+cu118",
        "tag": "cp311-cp311-linux_x86_64",
    },
}
EXTERNAL_IDS = {
    "torch-cu118", "torchvision-cu118", "maskrcnn-weights", "yolo-weights",
    "compliance-hf-cache", "compliance-reranker",
}


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        raise FileNotFoundError(f"artifact manifest missing: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def refresh_manifest(manifest: dict[str, Any]) -> None:
    for item in manifest.get("artifacts", []):
        path = ROOT / item["path"]
        if item.get("kind", "").endswith("directory") and path.is_dir():
            count, size, digest = tree_digest(path)
            if count:
                item["file_count"] = count
                item["size_bytes"] = size
                item["sha256"] = digest
                item["status"] = "verified-local"
            else:
                item["file_count"] = None
                item["size_bytes"] = None
                item["sha256"] = None
                item["status"] = "external-not-bundled"
        elif path.is_file():
            item["size_bytes"] = path.stat().st_size
            item["sha256"] = sha256_file(path)
            item["status"] = "verified-local"
        else:
            item["file_count"] = None if item.get("kind", "").endswith("directory") else item.get("file_count")
            item["size_bytes"] = None
            item["sha256"] = None
            item["status"] = (
                "external-not-bundled" if item.get("id") in EXTERNAL_IDS else "missing"
            )
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )




def inspect_wheel(path: Path, expected: dict[str, Any]) -> str | None:
    """Return an error string when wheel metadata/tag do not match the target."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
            wheel_path = next(name for name in names if name.endswith(".dist-info/WHEEL"))
            metadata = email.message_from_bytes(archive.read(metadata_path))
            wheel_text = archive.read(wheel_path).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, StopIteration, OSError) as exc:
        return f"invalid wheel archive: {type(exc).__name__}: {exc}"
    actual_name = str(metadata.get("Name", "")).lower().replace("-", "_")
    expected_name = str(expected["distribution"]).lower().replace("-", "_")
    if actual_name != expected_name:
        return f"wheel distribution mismatch: {actual_name!r} != {expected_name!r}"
    actual_version = str(metadata.get("Version", ""))
    if actual_version != expected["version"]:
        return f"wheel version mismatch: {actual_version!r} != {expected['version']!r}"
    tags = {line.split(":", 1)[1].strip() for line in wheel_text.splitlines() if line.startswith("Tag:")}
    if expected["tag"] not in tags:
        return f"wheel tag mismatch: expected {expected['tag']}, found {sorted(tags)}"
    return None

def result(name: str, status: str, detail: str, *, category: str = "code") -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "category": category}


def run_import_probe(
    label: str,
    code: str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 45,
) -> dict[str, str]:
    merged = os.environ.copy()
    merged.update(env or {})
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=cwd,
            env=merged,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return result(label, "fail", f"import timed out after {timeout}s", category="environment")
    if proc.returncode == 0:
        last = (proc.stdout.strip().splitlines() or ["import ok"])[-1]
        return result(label, "pass", last, category="environment")
    msg = (proc.stderr or proc.stdout).strip().splitlines()
    return result(label, "fail", msg[-1] if msg else f"exit={proc.returncode}", category="environment")


def validate_compose() -> dict[str, str]:
    compose = ROOT / "docker-compose.yml"
    try:
        import yaml
        data = yaml.safe_load(compose.read_text(encoding="utf-8"))
        services = data.get("services", {})
        required = {"floorplan-api", "redis", "compliance-api", "compliance-worker"}
        missing = sorted(required - set(services))
        if missing:
            return result("compose-structure", "fail", f"missing services: {missing}")
        fp_profiles = set(services["floorplan-api"].get("profiles", []))
        full_profiles = set(services["compliance-api"].get("profiles", []))
        if "floorplan-only" not in fp_profiles or "full-pipeline" not in fp_profiles:
            return result("compose-structure", "fail", "floorplan-api profiles are incomplete")
        if "full-pipeline" not in full_profiles:
            return result("compose-structure", "fail", "compliance-api lacks full-pipeline profile")
        return result("compose-structure", "pass", "YAML parsed and both execution profiles exist")
    except Exception as exc:  # preflight must report, not crash
        return result("compose-structure", "fail", f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("code", "floorplan-only", "full-pipeline"), default="code"
    )
    parser.add_argument("--strict", action="store_true", help="Treat missing external artifacts as failure")
    parser.add_argument("--refresh-manifest", action="store_true")
    parser.add_argument("--skip-runtime-imports", action="store_true", help="Validate source/workspace without loading ML/web runtimes")
    parser.add_argument("--artifacts-only", action="store_true", help="Seal/verify artifact files without checking the host Python runtime")
    parser.add_argument("--allow-environment-blockers", action="store_true", help="Return success when code checks pass but the audit host/runtime is blocked")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    checks: list[dict[str, str]] = []
    manifest = load_manifest()
    if args.refresh_manifest:
        refresh_manifest(manifest)
        manifest = load_manifest()

    if args.artifacts_only:
        checks.append(result(
            "python-version", "skip",
            f"host runtime skipped for artifact-only verification ({platform.python_version()})",
            category="environment",
        ))
    else:
        version_ok = sys.version_info[:2] == (3, 11)
        checks.append(result(
            "python-version",
            "pass" if version_ok else "fail",
            f"running {platform.python_version()}; required 3.11.x",
            category="environment",
        ))
    checks.append(result(
        "workspace-root", "pass" if (ROOT / "pyproject.toml").is_file() else "fail", str(ROOT)
    ))
    checks.append(validate_compose())
    lock_probe = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_dependency_locks.py")], cwd=ROOT, text=True, capture_output=True, check=False)
    checks.append(result("dependency-locks", "pass" if lock_probe.returncode == 0 else "fail", "hashed lock manifest verified" if lock_probe.returncode == 0 else (lock_probe.stderr or lock_probe.stdout)[-500:], category="code"))

    engine_root = ROOT / "compliance-engine"
    engine_required = args.mode == "full-pipeline"
    checks.append(result(
        "compliance-engine",
        "pass" if engine_root.is_dir() else ("fail" if engine_required else "skip"),
        str(engine_root),
        category="external" if not engine_root.is_dir() else "code",
    ))
    corpus = engine_root / "data" / "mabhas_clauses.json"
    checks.append(result(
        "regulation-corpus",
        "pass" if corpus.is_file() and corpus.stat().st_size > 1000 else ("fail" if engine_required else "skip"),
        str(corpus),
        category="external" if not corpus.is_file() else "code",
    ))

    required_ids: set[str] = set()
    if args.mode in {"floorplan-only", "full-pipeline"}:
        required_ids |= EXTERNAL_IDS
    if args.mode == "full-pipeline":
        required_ids.add("mabhas-corpus")

    for item in manifest.get("artifacts", []):
        artifact_id = item["id"]
        path = ROOT / item["path"]
        required = artifact_id in required_ids
        category = "external" if artifact_id in EXTERNAL_IDS else "code"
        is_directory = item.get("kind", "").endswith("directory")
        exists = path.is_dir() if is_directory else path.is_file()
        if not exists:
            checks.append(result(
                f"artifact:{artifact_id}",
                "fail" if required and args.strict else ("blocked" if required else "skip"),
                f"missing: {item['path']}",
                category=category,
            ))
            continue
        if is_directory:
            actual_count, actual_size, actual_sha = tree_digest(path)
            expected_count = item.get("file_count")
            if not actual_count:
                checks.append(result(f"artifact:{artifact_id}", "fail" if required and args.strict else ("blocked" if required else "skip"), "directory contains no sealed model files", category=category))
                continue
            if expected_count is None or item.get("size_bytes") is None or item.get("sha256") is None:
                checks.append(result(f"artifact:{artifact_id}", "fail" if required and args.strict else ("blocked" if required else "skip"), f"present but directory manifest is unsealed ({actual_count} files)", category=category))
            elif (actual_count, actual_size, actual_sha) != (expected_count, item["size_bytes"], item["sha256"]):
                checks.append(result(f"artifact:{artifact_id}", "fail", "directory tree SHA-256 mismatch", category=category))
            else:
                checks.append(result(f"artifact:{artifact_id}", "pass", f"{actual_count} files {actual_size} bytes sha256={actual_sha}", category=category))
            continue
        if artifact_id in EXPECTED_WHEELS:
            wheel_spec = EXPECTED_WHEELS[artifact_id]
            if not wheel_spec["filename"].match(path.name):
                checks.append(result(f"artifact:{artifact_id}", "fail", f"unexpected filename: {path.name}", category=category))
                continue
            wheel_error = inspect_wheel(path, wheel_spec)
            if wheel_error:
                checks.append(result(f"artifact:{artifact_id}", "fail", wheel_error, category=category))
                continue
        actual_size = path.stat().st_size
        expected_size = item.get("size_bytes")
        expected_sha = item.get("sha256")
        if expected_size is None or expected_sha is None:
            checks.append(result(
                f"artifact:{artifact_id}",
                "fail" if args.strict else "blocked",
                f"present but manifest is unsealed; run --refresh-manifest ({actual_size} bytes)",
                category=category,
            ))
            continue
        actual_sha = sha256_file(path)
        if actual_size != expected_size or actual_sha != expected_sha:
            checks.append(result(
                f"artifact:{artifact_id}", "fail", "size or SHA-256 mismatch", category=category
            ))
        else:
            checks.append(result(
                f"artifact:{artifact_id}", "pass", f"{actual_size} bytes sha256={actual_sha}", category=category
            ))

    # Distribution imports. Missing optional/full-runtime packages are environment failures,
    # never silently converted into code failures.
    if args.artifacts_only:
        for name in ("import:ifcopenshell", "import:torch", "import:torchvision", "import:tensorflow", "stage1-core-import", "stage1-api-import", "engine-api-import"):
            checks.append(result(name, "skip", "artifact-only verification", category="environment"))
    else:
        expected_versions = {
            "ifcopenshell": lambda v: v.startswith("0.8."),
            "torch": lambda v: v.startswith("2.1.2"),
            "torchvision": lambda v: v.startswith("0.16.2"),
            "tensorflow": lambda v: v.startswith("2.14."),
        }
        import_targets = ["ifcopenshell"] if args.skip_runtime_imports else ["ifcopenshell", "torch", "torchvision", "tensorflow"]
        for module in import_targets:
            try:
                version = importlib.metadata.version(module)
                matches = expected_versions[module](version)
                status = "pass" if matches else ("blocked" if args.mode == "code" else "fail")
                detail = version if matches else f"installed {version}; target version does not match project pins"
            except importlib.metadata.PackageNotFoundError:
                status = "fail" if args.mode != "code" else "blocked"
                detail = "distribution not installed"
            checks.append(result(f"import:{module}", status, detail, category="environment"))

        checks.append(run_import_probe(
            "stage1-core-import",
            "from export.ifc_exporter import bim_json_to_ifc; import schemas; print('stage1 core import ok')",
            cwd=ROOT,
        ))
        if args.skip_runtime_imports:
            checks.append(result("stage1-api-import", "skip", "runtime imports explicitly skipped", category="environment"))
        else:
            checks.append(run_import_probe(
                "stage1-api-import",
                "import application; print('application import ok')",
                cwd=ROOT,
                env={"APP_ENV": "development", "APP_CORS_ORIGINS": "*"},
            ))
        if engine_root.is_dir():
            checks.append(run_import_probe(
                "engine-api-import",
                "from api.main import app; print(app.title)",
                cwd=engine_root,
            ))

    counts = {key: sum(1 for c in checks if c["status"] == key) for key in ("pass", "blocked", "skip", "fail")}
    external_blockers = [c for c in checks if c["status"] == "blocked" and c["category"] == "external"]
    code_failures = [c for c in checks if c["status"] == "fail" and c["category"] == "code"]
    environment_failures = [c for c in checks if c["status"] == "fail" and c["category"] == "environment"]
    overall = "pass"
    if code_failures or (args.strict and counts["fail"]):
        overall = "fail"
    elif environment_failures or external_blockers or counts["fail"] or counts["blocked"]:
        overall = "blocked"

    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "strict": args.strict,
        "artifacts_only": args.artifacts_only,
        "overall": overall,
        "counts": counts,
        "checks": checks,
        "external_blockers": [c["name"] for c in external_blockers],
        "code_failures": [c["name"] for c in code_failures],
        "environment_failures": [c["name"] for c in environment_failures],
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        output = args.json_out if args.json_out.is_absolute() else ROOT / args.json_out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    if args.allow_environment_blockers and not code_failures and not (args.strict and counts["fail"]):
        return 0
    return 0 if overall == "pass" else (2 if overall == "blocked" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
