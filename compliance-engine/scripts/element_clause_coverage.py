"""
scripts/element_clause_coverage.py
==================================
Stage 3, Step 3 — element/clause coverage report on the real regulation graph.

For every canonical Element type, print the number of governing clauses and
the number after HAS_EXCEPTION expansion, plus which SpatialGraph category it
joins to. Demonstrates the GraphLinker on the Step 2 artifact.

Usage:
    python -m scripts.element_clause_coverage \
        [--graph data/regulation_graph.graphml] \
        [--out docs/element_clause_coverage.txt]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rag.build_regulation_graph import CANONICAL_ELEMENTS
from services.graph_linker import GraphLinker


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--graph", default="data/regulation_graph.graphml")
    p.add_argument("--out", default="docs/element_clause_coverage.txt")
    args = p.parse_args()

    linker = GraphLinker(regulation_graph_path=args.graph)

    lines = [
        "ELEMENT -> CLAUSE COVERAGE (GraphLinker on the real regulation graph)",
        "=" * 72,
        f"{'element':<14} {'spatial category':<16} {'clauses':>8} {'+exceptions':>12}",
        "-" * 72,
    ]
    total_base = total_exp = 0
    for element in sorted(CANONICAL_ELEMENTS):
        base = linker.clauses_for_element(element, include_exceptions=False)
        expanded = linker.clauses_for_element(element, include_exceptions=True)
        total_base += len(base)
        total_exp += len(expanded)
        category = CANONICAL_ELEMENTS[element] or "—"
        gained = len(expanded) - len(base)
        marker = f"  (+{gained} via HAS_EXCEPTION)" if gained else ""
        lines.append(f"{element:<14} {category:<16} {len(base):>8}"
                     f" {len(expanded):>12}{marker}")
    lines += [
        "-" * 72,
        f"{'TOTAL (clause-element links)':<31} {total_base:>8} {total_exp:>12}",
        "",
        "Notes: 'clauses' counts distinct clauses with a GOVERNS edge to the",
        "element. '+exceptions' adds HAS_EXCEPTION children of those clauses.",
        "RELATES-only links are intentionally excluded (they relate, not govern).",
    ]
    report = "\n".join(lines) + "\n"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"saved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
