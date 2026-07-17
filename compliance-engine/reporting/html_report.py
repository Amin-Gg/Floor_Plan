"""Self-contained HTML renderer for ValidationReport v1.0.

This module performs presentation only.  It never computes validation status,
re-runs a check, or reads raw IFC/model dictionaries.
"""
from __future__ import annotations

import html
from typing import Any, Mapping

from .report_model import ValidationReport

_VERDICT_STYLE = {
    "FAIL": {"border": "#E24B4A", "bg": "#FCEBEB", "title": "#A32D2D", "body": "#791F1F"},
    "NEEDS_REVIEW": {"border": "#EF9F27", "bg": "#FAEEDA", "title": "#854F0B", "body": "#633806"},
    "NOT_EVALUATED": {"border": "#888780", "bg": "#F1EFE8", "title": "#444441", "body": "#5F5E5A"},
    "PASS": {"border": "#1D9E75", "bg": "#E1F5EE", "title": "#0F6E56", "body": "#085041"},
    "NOT_APPLICABLE": {"border": "#B4B2A9", "bg": "#F6F5F2", "title": "#5F5E5A", "body": "#5F5E5A"},
}
_OVERALL_STYLE = {
    "success": {"bg": "#E1F5EE", "fg": "#0F6E56"},
    "warning": {"bg": "#FAEEDA", "fg": "#854F0B"},
    "error": {"bg": "#FCEBEB", "fg": "#A32D2D"},
    "incomplete": {"bg": "#F1EFE8", "fg": "#444441"},
}
_STAGE_STYLE = {
    "passed": {"bg": "#E1F5EE", "fg": "#0F6E56"},
    "passed_with_alerts": {"bg": "#FAEEDA", "fg": "#854F0B"},
    "failed": {"bg": "#FCEBEB", "fg": "#A32D2D"},
    "completed": {"bg": "#E1F5EE", "fg": "#0F6E56"},
    "completed_with_review": {"bg": "#FAEEDA", "fg": "#854F0B"},
    "blocked": {"bg": "#F1EFE8", "fg": "#444441"},
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _finding_card(finding: Mapping[str, Any]) -> str:
    verdict = str(finding.get("verdict") or "NEEDS_REVIEW")
    style = _VERDICT_STYLE.get(verdict, _VERDICT_STYLE["NEEDS_REVIEW"])
    code = finding.get("code") or finding.get("clause_id") or finding.get("article_id") or "?"
    rule = finding.get("clause_text") or finding.get("rule_text_en") or finding.get("requirement") or ""
    message = finding.get("message") or ""
    title = rule if rule else message
    if len(str(title)) > 100:
        title = str(title)[:99] + "…"
    anchor = finding.get("element_ifc_guid") or finding.get("element_internal_id")
    identity = f" · element {_e(anchor)}" if anchor else ""
    expected = finding.get("expected")
    actual = finding.get("actual")
    unit = finding.get("unit")
    measured = ""
    if expected is not None or actual is not None:
        measured = (
            '<p class="fmeta">expected '
            f'{_e(expected)} · actual {_e(actual)}'
            f'{(" " + _e(unit)) if unit else ""}</p>'
        )
    badge = ""
    if verdict == "NEEDS_REVIEW" and finding.get("unsupported"):
        badge = '<span class="small-badge">manual — outside engine scope</span>'
    return f"""
      <div class="finding" style="border-left:3px solid {style['border']}; background:{style['bg']};">
        <div class="finding-head">
          <span class="finding-title" style="color:{style['title']};">{_e(title)}{badge}</span>
          <span class="finding-id" style="color:{style['title']};">{_e(code)}</span>
        </div>
        <p class="finding-msg" style="color:{style['body']};">{_e(message)}{identity}</p>
        {measured}
      </div>"""


def _stage_html(stage: Mapping[str, Any] | None, label: str) -> str:
    if stage is None:
        return ""
    if stage.get("skipped"):
        return f"""
  <p class="section-label">{_e(label)}</p>
  <div class="skip-box"><strong>Skipped.</strong> {_e(stage.get('skip_reason') or 'No reason recorded.')}</div>
"""
    status = str(stage.get("status") or "unknown")
    style = _STAGE_STYLE.get(status, {"bg": "#F1EFE8", "fg": "#444441"})
    findings = list(stage.get("findings") or [])
    cards = "\n".join(_finding_card(f) for f in findings)
    if not cards:
        cards = '<p class="empty">No findings.</p>'
    checker = stage.get("checker_version")
    checker_text = f" · checker {_e(checker)}" if checker else ""
    return f"""
  <p class="section-label">{_e(label)}</p>
  <p class="stage-line">
    <span class="stage-pill" style="background:{style['bg']}; color:{style['fg']};">{_e(status.replace('_', ' '))}</span>
    <span class="stage-meta">{len(findings)} finding{'s' if len(findings) != 1 else ''}{checker_text}</span>
  </p>
  {cards}
"""


def _coverage_html(coverage: Mapping[str, Any] | None) -> str:
    if not coverage:
        return ""
    total = int(coverage.get("total_clauses", 0) or 0)
    checkable = int(coverage.get("automatically_checkable", 0) or 0)
    pct = round(checkable / total * 100, 1) if total else 0.0
    rows = [
        ("Checked", coverage.get("checked", 0)),
        ("Passed", coverage.get("passed", 0)),
        ("Failed", coverage.get("failed", 0)),
        ("Needs review", coverage.get("needs_review", 0)),
        ("Blocked by missing data", coverage.get("blocked_by_missing_data", 0)),
        ("Unsupported", coverage.get("unsupported", 0)),
    ]
    body = "".join(
        f"<tr><td>{_e(label)}</td><td class='num'>{_e(value)}</td></tr>"
        for label, value in rows
    )
    return f"""
  <p class="section-label">clause coverage — what was automatically checkable</p>
  <table class="coverage">
    <tr><td>Clauses evaluated</td><td class="num">{total}</td></tr>
    <tr><td>Automatically checkable</td><td class="num">{checkable} ({pct}%)</td></tr>
    {body}
  </table>
  <p class="legend">Unsupported and blocked clauses are explicitly reported and require manual action.</p>
"""


def render_html(report: ValidationReport) -> str:
    data = report.to_dict()
    metadata = data.get("metadata") or {}
    model = data["model"]
    overall = data["overall"]
    overall_style = _OVERALL_STYLE.get(overall["status"], _OVERALL_STYLE["incomplete"])
    stages = data["stages"]
    compliance = stages.get("compliance") or {}
    compliance_findings = list(compliance.get("findings") or [])
    verdicts = {k: 0 for k in ("PASS", "FAIL", "NEEDS_REVIEW", "NOT_EVALUATED", "NOT_APPLICABLE")}
    for item in compliance_findings:
        verdict = str(item.get("verdict") or "")
        if verdict in verdicts:
            verdicts[verdict] += 1
    total = sum(verdicts.values())
    denominator = total or 1
    widths = {key: round(value / denominator * 100, 1) for key, value in verdicts.items()}
    n_unsupported = sum(
        1 for f in compliance_findings
        if f.get("verdict") == "NEEDS_REVIEW" and f.get("unsupported")
    )
    n_judgment = verdicts["NEEDS_REVIEW"] - n_unsupported
    review_split = (f"{n_judgment} judgment · {n_unsupported} outside scope"
                    if verdicts["NEEDS_REVIEW"] else "")
    compliance_cards = "\n".join(_finding_card(f) for f in compliance_findings)
    if not compliance_cards:
        compliance_cards = '<p class="empty">No compliance findings.</p>'

    plan_name = metadata.get("plan_name") or model.get("name") or "Floor plan"
    occupancy = metadata.get("occupancy") or "M-4 residential"
    standards = metadata.get("standards_versions") or {}
    reasons = "".join(f"<li>{_e(reason)}</li>" for reason in overall.get("reasons") or [])
    reasons_html = f"<ul class='reasons'>{reasons}</ul>" if reasons else ""
    skipped = data.get("skipped_stages") or {}
    skipped_html = ""
    if skipped:
        skipped_html = """
  <p class="section-label">skipped stages</p>
  <ul class="skipped-list">%s</ul>
""" % "".join(f"<li><strong>{_e(k)}:</strong> {_e(v)}</li>" for k, v in skipped.items())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Mabhas compliance report — {_e(plan_name)}</title>
<style>
  @page {{ size: A4; margin: 18mm 15mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color:#1a1a1a; margin:0; font-size:13px; line-height:1.55; }}
  .wrap {{ max-width:820px; margin:0 auto; padding:24px; }}
  .header {{ display:flex; justify-content:space-between; gap:20px; border-bottom:1px solid #e5e5e5; padding-bottom:16px; margin-bottom:20px; }}
  h1 {{ font-size:20px; font-weight:600; margin:0; }}
  .sub {{ color:#6b6b6b; margin:4px 0 0; }}
  .status {{ font-size:12px; font-weight:600; padding:6px 14px; border-radius:8px; white-space:nowrap; }}
  .reasons {{ margin:0 0 18px 18px; color:#5F5E5A; }}
  .metrics {{ display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin-bottom:18px; }}
  .metric {{ background:#f6f5f2; border-radius:8px; padding:12px 14px; }}
  .metric .label {{ font-size:11px; color:#6b6b6b; margin:0; }}
  .metric .value {{ font-size:25px; font-weight:600; margin:2px 0 0; }}
  .bar {{ display:flex; height:12px; border-radius:6px; overflow:hidden; margin-bottom:6px; background:#f1efe8; }}
  .legend {{ font-size:11px; color:#6b6b6b; margin:0 0 20px; }}
  .section-label {{ font-size:10px; color:#8a8a8a; text-transform:uppercase; letter-spacing:.06em; margin:22px 0 10px; }}
  .stage-line {{ margin:0 0 10px; }}
  .stage-pill {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; }}
  .stage-meta {{ font-size:11px; color:#8a8a8a; margin-left:6px; }}
  .finding {{ padding:11px 14px; margin-bottom:8px; page-break-inside:avoid; }}
  .finding-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; }}
  .finding-title {{ font-size:14px; font-weight:600; }}
  .finding-id {{ font-size:11px; font-family:Menlo,Consolas,monospace; white-space:nowrap; }}
  .finding-msg,.fmeta {{ font-size:12px; margin:4px 0 0; }}
  .fmeta {{ color:#6b6b6b; }}
  .small-badge {{ font-size:9px; text-transform:uppercase; padding:2px 6px; border-radius:10px; background:#EFE9DA; color:#6B5A2E; margin-left:8px; }}
  .coverage {{ border-collapse:collapse; width:100%; font-size:12px; background:#f6f5f2; }}
  .coverage td {{ padding:5px 10px; }} .coverage .num {{ text-align:right; font-weight:600; }}
  .skip-box {{ padding:10px 12px; background:#f1efe8; color:#444441; margin-bottom:12px; }}
  .skipped-list {{ color:#5F5E5A; margin-top:0; }}
  .empty {{ color:#6b6b6b; }}
  .footer {{ border-top:1px solid #e5e5e5; margin-top:24px; padding-top:14px; font-size:10px; color:#7a7a7a; }}
</style>
</head>
<body><div class="wrap">
  <div class="header">
    <div><h1>Mabhas compliance report</h1>
      <p class="sub">{_e(plan_name)} · {_e(occupancy)} · {_e(data['mode'])} · generated {_e(data['generated_at'])}</p></div>
    <span class="status" style="background:{overall_style['bg']}; color:{overall_style['fg']};">{_e(overall['label'])}</span>
  </div>
  {reasons_html}
  <div class="metrics">
    <div class="metric"><p class="label">checked</p><p class="value">{total}</p></div>
    <div class="metric"><p class="label" style="color:#0F6E56">pass</p><p class="value" style="color:#0F6E56">{verdicts['PASS']}</p></div>
    <div class="metric"><p class="label" style="color:#A32D2D">fail</p><p class="value" style="color:#A32D2D">{verdicts['FAIL']}</p></div>
    <div class="metric"><p class="label" style="color:#854F0B">review</p><p class="value" style="color:#854F0B">{verdicts['NEEDS_REVIEW']}</p><p class="label">{_e(review_split)}</p></div>
    <div class="metric"><p class="label">not evaluated</p><p class="value">{verdicts['NOT_EVALUATED']}</p></div>
  </div>

  {_stage_html(stages.get('schema'), 'stage 1 — ifc schema check')}
  {_stage_html(stages.get('quality'), 'stage 2 — model quality check')}
  <p class="section-label">stage 3 — mabhas compliance check</p>
  {('<div class="skip-box"><strong>Skipped.</strong> ' + _e(compliance.get('skip_reason')) + '</div>') if compliance.get('skipped') else ''}
  <div class="bar">
    <div style="width:{widths['PASS']}%;background:#1D9E75"></div>
    <div style="width:{widths['FAIL']}%;background:#E24B4A"></div>
    <div style="width:{widths['NEEDS_REVIEW']}%;background:#EF9F27"></div>
    <div style="width:{widths['NOT_EVALUATED']}%;background:#888780"></div>
  </div>
  <p class="legend">pass {widths['PASS']}% · fail {widths['FAIL']}% · review {widths['NEEDS_REVIEW']}% · not evaluated {widths['NOT_EVALUATED']}%</p>
  {_coverage_html(compliance.get('coverage'))}
  <p class="section-label">compliance findings — failures first, then review, not-evaluated, pass</p>
  {compliance_cards}
  {skipped_html}

  <div class="footer">
    Report schema {_e(data['report_schema_version'])} · engine {_e(data['engine_version'])} · run {_e(data['run_id'])}.<br/>
    Semantic catalog {_e(standards.get('semantic_catalog'))} · controlled values {_e(standards.get('controlled_values'))}.<br/>
    PASS/FAIL decisions are deterministic. RAG/LLM content is advisory on eligible review items only and never changes a deterministic verdict. This report is decision support and does not replace professional certification.
  </div>
</div></body></html>"""


__all__ = ["render_html"]
