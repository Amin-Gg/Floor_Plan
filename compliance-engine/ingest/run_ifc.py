"""
ingest/run_ifc.py
=================
CLI: run a Step-1 enriched IFC through the compliance engine and write reports.

    python -m ingest.run_ifc plan.ifc
    python -m ingest.run_ifc plan.ifc --clauses data/mabhas_clauses.json --out ifc_reports
    python -m ingest.run_ifc plan.ifc --threshold 0.6 --no-reports

Prints the verdict summary, how many elements were flagged / verdicts
downgraded for low confidence, the room categories the plan contained (so you
can see the category-vocabulary seam at a glance), and the report file paths.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Path bootstrap (root + services) happens on importing the pipeline.
from ingest.ifc_pipeline import run_ifc_compliance, _ROOT


def _default_clauses_path() -> str:
    return os.path.join(_ROOT, "data", "mabhas_clauses.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m ingest.run_ifc",
        description="Run an enriched IFC (Step 1 output) through the Mabhas "
                    "compliance engine.")
    ap.add_argument("ifc", help="path to the plan.ifc produced by Step 1")
    ap.add_argument("--clauses", default=None,
                    help="path to the Mabhas clauses JSON "
                         "(default: data/mabhas_clauses.json)")
    ap.add_argument("--out", default="ifc_reports",
                    help="directory for HTML/PDF/BCF reports (default: ifc_reports)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="confidence threshold for the review pre-pass "
                         "(default: env REVIEW_CONFIDENCE_THRESHOLD or 0.5)")
    ap.add_argument("--no-reports", action="store_true",
                    help="run compliance only; do not generate report files")
    ap.add_argument("--json", action="store_true",
                    help="print the full result as JSON to stdout")
    args = ap.parse_args(argv)

    clauses_path = args.clauses or _default_clauses_path()
    from api.pipeline import load_clauses
    clauses = load_clauses(clauses_path)
    if not clauses:
        print(f"⚠ No clauses loaded from {clauses_path} — verdicts will be empty. "
              f"Pass --clauses with a valid Mabhas corpus.", file=sys.stderr)
    # raw corpus size (pre skip_category filter) for honest coverage context
    corpus_total = None
    try:
        import json as _json
        corpus_total = len(_json.load(open(clauses_path, encoding="utf-8")))
    except Exception:
        pass

    result, bim_data = run_ifc_compliance(args.ifc, clauses, threshold=args.threshold,
                                          corpus_total=corpus_total)
    rs = bim_data.get("_review_summary", {})
    cov = bim_data.get("_coverage", {})
    cat = bim_data.get("_category_summary", {})

    # ── console summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"IFC:        {args.ifc}")
    print(f"Schema:     {bim_data.get('schema_version')} | units "
          f"{bim_data.get('units')}")
    print(f"Clauses:    {len(clauses)} loaded from {os.path.basename(clauses_path)}")
    print(f"Elements:   {len(bim_data.get('walls', []))} walls, "
          f"{len(bim_data.get('doors', []))} doors, "
          f"{len(bim_data.get('windows', []))} windows, "
          f"{len(bim_data.get('rooms', []))} rooms")
    print(f"Verdicts:   {result.summary}")
    print(f"Findings:   {len(result.findings)}")
    print(f"Flagged:    {rs.get('flagged_count', 0)} uncertain element(s); "
          f"{rs.get('downgraded_count', 0)} verdict(s) downgraded to NEEDS_REVIEW")
    print(f"Rooms:      {cat.get('canonical', 0)} canonical, "
          f"{cat.get('normalized', 0)} normalized, {cat.get('unmapped', 0)} unmapped"
          + (f"  unmapped_raw={cat.get('unmapped_raw')}" if cat.get('unmapped') else ""))
    print("-" * 60)
    print("Coverage (clauses):")
    print(f"  evaluated:               {cov.get('total_clauses', 0)}"
          + (f"   (corpus {cov.get('corpus_total')}, "
             f"{cov.get('not_applicable', 0)} n/a)" if cov.get('corpus_total') else ""))
    print(f"  automatically checkable: {cov.get('automatically_checkable', 0)}")
    print(f"    checked:               {cov.get('checked', 0)} "
          f"(pass {cov.get('passed', 0)}, fail {cov.get('failed', 0)}, "
          f"review {cov.get('needs_review', 0)})")
    print(f"    blocked (missing data):{cov.get('blocked_by_missing_data', 0)}")
    print(f"  unsupported:             {cov.get('unsupported', 0)}")
    print("=" * 60)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    if not args.no_reports:
        from services.report_generator import generate_reports
        meta = {"plan_name": os.path.basename(args.ifc)}
        paths = generate_reports(result.to_dict(), meta, out_dir=args.out,
                                 coverage=cov)
        print("\nReports written:")
        for kind, path in paths.items():
            print(f"  {kind:4s}: {path if path else '(skipped)'}")

    # exit non-zero if any hard FAIL, so this is CI/script friendly
    return 1 if result.summary.get("FAIL", 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
