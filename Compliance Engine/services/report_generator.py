"""
services/report_generator.py
============================
Step 11 — Report Generation

Takes the ComplianceResult from the orchestrator (Step 8) and produces three
deliverables from one fixed template:

    generate_reports(result_dict, meta, out_dir) -> dict of file paths
      - <out>/compliance_report.html   self-contained, prints cleanly
      - <out>/compliance_report.pdf    real PDF (WeasyPrint, matches the HTML)
      - <out>/compliance_issues.bcf    BCF 2.1 for BIM tools (zip of markup XML)

Design
------
* FIXED STRUCTURE, data-bound. The layout never changes; only the findings,
  counts, and metadata are filled in. This makes the output reliable and easy
  to restyle in one place (the HTML template string).
* Self-contained HTML — all CSS inline in a <style> block, no external assets,
  so it opens anywhere and prints to PDF identically.
* PDF is rendered FROM the HTML via WeasyPrint, so the two always match. If
  WeasyPrint is unavailable, PDF generation is skipped gracefully (HTML + BCF
  still produced) and a note is returned.
* BCF (BIM Collaboration Format) 2.1 — each FAIL/NEEDS_REVIEW finding becomes a
  topic a reviewer can open in any BCF-capable BIM viewer (Revit, BIMcollab,
  That Open). PASS findings are omitted from BCF (only issues need tracking).

No LLM. Pure templating + a zip for BCF.
"""

from __future__ import annotations

import html
import os
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── verdict → colour mapping (used in HTML) ──────────────────────────────────
_VERDICT_STYLE = {
    "FAIL":         {"border": "#E24B4A", "bg": "#FCEBEB", "title": "#A32D2D", "body": "#791F1F", "label": "fail"},
    "NEEDS_REVIEW": {"border": "#EF9F27", "bg": "#FAEEDA", "title": "#854F0B", "body": "#633806", "label": "review"},
    "PASS":         {"border": "#1D9E75", "bg": "#E1F5EE", "title": "#0F6E56", "body": "#085041", "label": "pass"},
}
# Order findings appear in the report: failures first, then review, then pass.
_ORDER = {"FAIL": 0, "NEEDS_REVIEW": 1, "PASS": 2}


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════

def generate_reports(
    result: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
    out_dir: str = ".",
) -> Dict[str, Optional[str]]:
    """
    Generate HTML + PDF + BCF from a ComplianceResult.to_dict().

    Parameters
    ----------
    result : dict   — ComplianceResult.to_dict()
    meta   : dict   — optional {plan_name, occupancy, date}; sensible defaults used
    out_dir: str    — directory to write files into

    Returns
    -------
    dict with keys html/pdf/bcf → file paths (pdf may be None if WeasyPrint absent)
    """
    os.makedirs(out_dir, exist_ok=True)
    meta = _fill_meta(meta)
    summary = result.get("summary", {})
    findings = sorted(
        result.get("findings", []),
        key=lambda f: (_ORDER.get(f.get("verdict"), 3), str(f.get("article_id"))),
    )

    # 1. HTML
    html_str = _build_html(summary, findings, meta, result.get("duration_s"))
    html_path = os.path.join(out_dir, "compliance_report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_str)

    # 2. PDF (from the HTML)
    pdf_path: Optional[str] = os.path.join(out_dir, "compliance_report.pdf")
    try:
        from weasyprint import HTML as WeasyHTML
        WeasyHTML(string=html_str).write_pdf(pdf_path)
    except Exception as exc:
        pdf_path = None
        print(f"PDF generation skipped (WeasyPrint unavailable: {exc})")

    # 3. BCF (issues only)
    bcf_path = os.path.join(out_dir, "compliance_issues.bcf")
    _build_bcf(findings, meta, bcf_path)

    return {"html": html_path, "pdf": pdf_path, "bcf": bcf_path}


# ═══════════════════════════════════════════════════════════════════════════
# Metadata + overall status
# ═══════════════════════════════════════════════════════════════════════════

def _fill_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    meta = dict(meta or {})
    meta.setdefault("plan_name", "Floor plan")
    meta.setdefault("occupancy", "M-4 residential")
    meta.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    return meta


def _overall_status(summary: Dict[str, int]) -> Dict[str, str]:
    if summary.get("FAIL", 0) > 0:
        return {"label": "non-compliant", "bg": "#FCEBEB", "fg": "#A32D2D"}
    if summary.get("NEEDS_REVIEW", 0) > 0:
        return {"label": "needs review", "bg": "#FAEEDA", "fg": "#854F0B"}
    return {"label": "compliant", "bg": "#E1F5EE", "fg": "#0F6E56"}


# ═══════════════════════════════════════════════════════════════════════════
# HTML template (fixed structure, data-bound)
# ═══════════════════════════════════════════════════════════════════════════

def _build_html(summary, findings, meta, duration_s) -> str:
    n_pass = summary.get("PASS", 0)
    n_fail = summary.get("FAIL", 0)
    n_rev  = summary.get("NEEDS_REVIEW", 0)
    total  = n_pass + n_fail + n_rev or 1
    status = _overall_status(summary)

    # compliance bar widths (percent)
    w_pass = round(n_pass / total * 100, 1)
    w_fail = round(n_fail / total * 100, 1)
    w_rev  = round(n_rev  / total * 100, 1)

    # finding cards
    cards = []
    for f in findings:
        v = f.get("verdict", "NEEDS_REVIEW")
        st = _VERDICT_STYLE.get(v, _VERDICT_STYLE["NEEDS_REVIEW"])
        article = html.escape(str(f.get("article_id", "?")))
        msg     = html.escape(str(f.get("message", "")))
        # derive a short title from the rule text if present, else the message
        rule    = f.get("rule_text_en") or ""
        title   = html.escape((rule[:70] + "…") if len(rule) > 70 else (rule or msg[:70]))
        tag     = html.escape(str(f.get("object") or v.lower()))
        cards.append(f"""
      <div class="finding" style="border-left:3px solid {st['border']}; background:{st['bg']};">
        <div class="finding-head">
          <span class="finding-title" style="color:{st['title']};">{title}</span>
          <span class="finding-id" style="color:{st['title']};">{article}</span>
        </div>
        <p class="finding-msg" style="color:{st['body']};">{msg}</p>
      </div>""")
    cards_html = "\n".join(cards) if cards else "<p>No findings.</p>"

    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    dur = f"{duration_s:.2f}s" if isinstance(duration_s, (int, float)) else "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Mabhas compliance report — {html.escape(meta['plan_name'])}</title>
<style>
  @page {{ size: A4; margin: 18mm 15mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: #1a1a1a; margin: 0; padding: 0; font-size: 13px; line-height: 1.6; }}
  .wrap {{ max-width: 820px; margin: 0 auto; padding: 24px; }}
  .header {{ display:flex; justify-content:space-between; align-items:flex-start;
            border-bottom: 1px solid #e5e5e5; padding-bottom: 16px; margin-bottom: 20px; }}
  .header h1 {{ font-size: 20px; font-weight: 600; margin: 0; }}
  .header .sub {{ font-size: 13px; color: #6b6b6b; margin: 4px 0 0; }}
  .status {{ font-size: 12px; font-weight: 600; padding: 6px 14px; border-radius: 8px; white-space: nowrap; }}
  .metrics {{ display:grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 18px; }}
  .metric {{ background:#f6f5f2; border-radius: 8px; padding: 12px 14px; }}
  .metric .label {{ font-size: 12px; color:#6b6b6b; margin:0; }}
  .metric .value {{ font-size: 26px; font-weight: 600; margin: 2px 0 0; }}
  .barlabel {{ font-size: 10px; color:#9a9a9a; text-transform:uppercase; letter-spacing:0.05em; margin: 0 0 6px; }}
  .bar {{ display:flex; height: 12px; border-radius: 6px; overflow:hidden; margin-bottom: 6px; }}
  .legend {{ font-size: 11px; color:#6b6b6b; margin: 0 0 22px; }}
  .legend span {{ margin-right: 14px; }}
  .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align: middle; }}
  .section-label {{ font-size: 10px; color:#9a9a9a; text-transform:uppercase; letter-spacing:0.05em; margin: 0 0 12px; }}
  .finding {{ border-radius: 0; padding: 11px 14px; margin-bottom: 8px; page-break-inside: avoid; }}
  .finding-head {{ display:flex; justify-content:space-between; align-items:baseline; gap: 12px; }}
  .finding-title {{ font-size: 14px; font-weight: 600; }}
  .finding-id {{ font-size: 12px; font-family: "SF Mono", Menlo, Consolas, monospace; white-space: nowrap; }}
  .finding-msg {{ font-size: 12px; margin: 4px 0 0; }}
  .footer {{ border-top: 1px solid #e5e5e5; margin-top: 24px; padding-top: 14px;
            font-size: 11px; color:#8a8a8a; line-height: 1.7; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div>
      <h1>Mabhas compliance report</h1>
      <p class="sub">{html.escape(meta['plan_name'])} · {html.escape(meta['occupancy'])} · generated {gen_time}</p>
    </div>
    <span class="status" style="background:{status['bg']}; color:{status['fg']};">{status['label']}</span>
  </div>

  <div class="metrics">
    <div class="metric"><p class="label">checked</p><p class="value">{total if findings else 0}</p></div>
    <div class="metric"><p class="label" style="color:#0F6E56;">pass</p><p class="value" style="color:#0F6E56;">{n_pass}</p></div>
    <div class="metric"><p class="label" style="color:#A32D2D;">fail</p><p class="value" style="color:#A32D2D;">{n_fail}</p></div>
    <div class="metric"><p class="label" style="color:#854F0B;">review</p><p class="value" style="color:#854F0B;">{n_rev}</p></div>
  </div>

  <p class="barlabel">compliance overview</p>
  <div class="bar">
    <div style="width:{w_pass}%; background:#1D9E75;"></div>
    <div style="width:{w_fail}%; background:#E24B4A;"></div>
    <div style="width:{w_rev}%; background:#EF9F27;"></div>
  </div>
  <p class="legend">
    <span><span class="dot" style="background:#1D9E75;"></span>pass {w_pass}%</span>
    <span><span class="dot" style="background:#E24B4A;"></span>fail {w_fail}%</span>
    <span><span class="dot" style="background:#EF9F27;"></span>review {w_rev}%</span>
  </p>

  <p class="section-label">findings — failures first, then review, then pass</p>
  {cards_html}

  <div class="footer">
    <strong>Methodology.</strong> Numeric and spatial verdicts (PASS/FAIL) are produced by
    deterministic checks against the building model. Rules that depend on information not
    derivable from the plan — site conditions, interpretive requirements — are flagged
    "review" for a qualified professional to confirm; they are never auto-judged.
    Scope: {html.escape(meta['occupancy'])}. Engine runtime: {dur}.
    This report is a decision-support tool and does not replace professional certification.
  </div>

</div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
# BCF 2.1 generation (issues only)
# ═══════════════════════════════════════════════════════════════════════════

def _build_bcf(findings: List[Dict[str, Any]], meta: Dict[str, Any],
               bcf_path: str) -> None:
    """
    Write a minimal BCF 2.1 file: a zip containing bcf.version + one folder per
    issue topic (FAIL and NEEDS_REVIEW only), each with a markup.bcf XML.
    """
    issues = [f for f in findings if f.get("verdict") in ("FAIL", "NEEDS_REVIEW")]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with zipfile.ZipFile(bcf_path, "w", zipfile.ZIP_DEFLATED) as z:
        # bcf.version (required at root)
        z.writestr("bcf.version",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Version VersionId="2.1" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<DetailedVersion>2.1</DetailedVersion></Version>')

        for f in issues:
            topic_guid = str(uuid.uuid4())
            verdict = f.get("verdict", "NEEDS_REVIEW")
            # BCF topic status mapping
            status = "Error" if verdict == "FAIL" else "Info"
            priority = "High" if verdict == "FAIL" else "Normal"
            title = _x(f"[{f.get('article_id','?')}] {f.get('message','')}"[:200])
            desc  = _x(f.get("rule_text_en") or f.get("message", ""))

            markup = f"""<?xml version="1.0" encoding="UTF-8"?>
<Markup xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Topic Guid="{topic_guid}" TopicType="Issue" TopicStatus="{status}">
    <Title>{title}</Title>
    <Priority>{priority}</Priority>
    <CreationDate>{now}</CreationDate>
    <CreationAuthor>Mabhas Compliance Engine</CreationAuthor>
    <Description>{desc}</Description>
  </Topic>
</Markup>"""
            z.writestr(f"{topic_guid}/markup.bcf", markup)


def _x(s: Any) -> str:
    """XML-escape."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
