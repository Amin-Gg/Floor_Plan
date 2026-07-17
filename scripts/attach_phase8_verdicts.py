#!/usr/bin/env python3
"""Attach compliance-engine verdicts to a Phase-8 annotation/prediction JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ORDER = {"NOT_EVALUATED": 0, "PASS": 1, "NEEDS_REVIEW": 2, "FAIL": 3}


def extract(payload: dict) -> dict[str, str]:
    compliance = payload.get("compliance") if isinstance(payload.get("compliance"), dict) else payload
    findings = compliance.get("findings", []) if isinstance(compliance, dict) else []
    verdicts: dict[str, str] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        rule_id = finding.get("clause_id") or finding.get("article_id") or finding.get("code") or finding.get("finding_id")
        verdict = str(finding.get("verdict") or "NOT_EVALUATED").upper()
        if not rule_id or verdict not in ORDER:
            continue
        key = str(rule_id)
        if key not in verdicts or ORDER[verdict] > ORDER[verdicts[key]]:
            verdicts[key] = verdict
    return verdicts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document", type=Path, required=True, help="Phase-8 annotations or predictions JSON")
    parser.add_argument("--engine-report", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    document = json.loads(args.document.read_text(encoding="utf-8"))
    report = json.loads(args.engine_report.read_text(encoding="utf-8"))
    verdicts = extract(report)
    document["verdicts"] = verdicts
    document.setdefault("verdict_provenance", {})
    document["verdict_provenance"].update({
        "engine_report": str(args.engine_report),
        "engine_report_sha256": __import__("hashlib").sha256(args.engine_report.read_bytes()).hexdigest(),
        "rules_attached": len(verdicts),
    })
    out = args.out or args.document
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "rules_attached": len(verdicts)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
