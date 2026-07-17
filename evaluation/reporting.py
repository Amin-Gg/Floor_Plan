"""Human-readable Phase-8 report rendering."""
from __future__ import annotations

from typing import Any


def render_markdown(report: dict[str, Any], gate: dict[str, Any] | None = None) -> str:
    lines = [
        f"# ML Evaluation — {report['dataset']['id']} / {report['variant']}",
        "",
        f"- Split: `{report['dataset']['split']}`",
        f"- Samples: **{report['summary']['samples']}**",
        f"- Ground-truth instances: **{report['summary']['instances']}**",
        f"- Empirical claims allowed: **{str(report['empirical_claims_allowed']).lower()}**",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Macro precision | {report['summary']['macro_precision']:.4f} |",
        f"| Macro recall | {report['summary']['macro_recall']:.4f} |",
        f"| Macro F1 | {report['summary']['macro_f1']:.4f} |",
        f"| mAP@0.50 | {report['summary']['map_50']:.4f} |",
        f"| mAP@0.75 | {report['summary']['map_75']:.4f} |",
        f"| mAP@0.50:0.95 | {report['summary']['map_50_95']:.4f} |",
        f"| ECE | {report['calibration_overall']['ece'] if report['calibration_overall']['ece'] is not None else 'n/a'} |",
        f"| Verdict exact agreement | {report['verdict_impact']['exact_agreement']:.4f} |",
        f"| Critical false PASS | {report['verdict_impact']['critical_false_pass']} |",
        "",
        "## Per class",
        "",
        "| Class | Support | Precision | Recall | F1 | AP50 | AP75 | mAP50:95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in report["classes"].items():
        lines.append(
            f"| {name} | {row['support']} | {row['precision']:.4f} | {row['recall']:.4f} | "
            f"{row['f1']:.4f} | {row['ap50']:.4f} | {row['ap75']:.4f} | {row['map_50_95']:.4f} |"
        )
    if gate is not None:
        lines.extend(["", "## Release gate", "", f"Overall: **{'PASS' if gate['passed'] else 'BLOCKED'}**", "", "| Check | Result | Actual | Expected |", "|---|---|---:|---:|"])
        for row in gate["checks"]:
            lines.append(f"| {row['name']} | {'PASS' if row['passed'] else 'FAIL'} | {row['actual']} | {row['expected']} |")
    if not report["empirical_claims_allowed"]:
        lines.extend([
            "",
            "> **Important:** This dataset is not human-verified. Numbers in this report validate the evaluator contract only and must not be presented as real model accuracy.",
        ])
    return "\n".join(lines) + "\n"
