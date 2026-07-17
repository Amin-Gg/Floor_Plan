"""PDF renderer for the exact HTML produced from ValidationReport."""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def write_pdf_report(html_text: str, path: str | Path) -> Optional[str]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from weasyprint import HTML as WeasyHTML
        WeasyHTML(string=html_text).write_pdf(str(target))
        return str(target)
    except Exception as exc:  # optional dependency/runtime surface
        print(f"PDF generation skipped (WeasyPrint unavailable: {exc})")
        return None


__all__ = ["write_pdf_report"]
