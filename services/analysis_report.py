"""
services/analysis_report.py
===========================
Tracks events that happen during /analyze processing so the API response can
include a structured report of what was created, what was skipped (and why),
and what degraded states occurred.

Design principles
-----------------
1. **Additive, never destructive**: methods only record events; nothing in the
   route logic should change behavior based on the tracker's state. If this
   class is removed, the route still works.

2. **Self-contained**: no imports from other project modules. The tracker has
   no opinions about routing, models, or BIM — just receives string events.

3. **Crash-safe**: every method swallows its own errors. If recording an event
   fails for any reason, the route continues. Reporting code must never break
   a working request.

4. **Cheap to construct and pass around**: the tracker is a single object you
   create once at the top of the route handler and pass to whatever code wants
   to record events. It carries only Python primitives in its internal state.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AnalysisReport:
    """Collects structured events for inclusion in the API response."""

    def __init__(self) -> None:
        # ── Element counts (set explicitly at the end of processing) ──────────
        self._elements: Dict[str, int] = {}

        # ── Per-stage status ──────────────────────────────────────────────────
        # Stage names are conventional strings: "ocr", "rooms", "stairs",
        # "slabs", "host_walls", "building_params". Each maps to one of:
        #   "ok"        — finished successfully
        #   "degraded"  — finished but with reduced output (with reason)
        #   "skipped"   — did not run (with reason)
        #   "failed"    — raised an error (with reason)
        self._stages: Dict[str, Dict[str, str]] = {}

        # ── Skipped items (per-element record with reason) ────────────────────
        # E.g. {"type": "door", "id": "Door_3", "reason": "no host wall within 500 mm"}
        self._skipped: List[Dict[str, Any]] = []

        # ── Warnings (free-form strings) ─────────────────────────────────────
        # For things that aren't tied to a specific element — global concerns
        # like "input image was downsampled" or "OCR engine failed".
        self._warnings: List[str] = []

        # ── Mode flags ────────────────────────────────────────────────────────
        self._model_mode: str = "unknown"
        self._ocr_used:   bool = False

    # ── Public recording API ──────────────────────────────────────────────────

    def set_model_mode(self, mode: str) -> None:
        """Record which model is serving this request, e.g. 'fine_tuned' or 'coco_fallback'."""
        try:
            self._model_mode = str(mode)
        except Exception:
            pass

    def set_ocr_used(self, used: bool) -> None:
        """Record whether OCR was attempted on this image."""
        try:
            self._ocr_used = bool(used)
        except Exception:
            pass

    def set_stage(self,
                  stage: str,
                  status: str,
                  reason: Optional[str] = None) -> None:
        """
        Record the outcome of a processing stage.

        Use one of these conventional status values:
            "ok", "degraded", "skipped", "failed"
        """
        try:
            entry: Dict[str, str] = {"status": str(status)}
            if reason:
                entry["reason"] = str(reason)
            self._stages[str(stage)] = entry
        except Exception:
            pass

    def add_skipped(self, element_type: str, reason: str, element_id: Optional[str] = None) -> None:
        """Record an element that was detected but not included in the BIM output."""
        try:
            entry: Dict[str, Any] = {
                "type":   str(element_type),
                "reason": str(reason),
            }
            if element_id is not None:
                entry["id"] = str(element_id)
            self._skipped.append(entry)
        except Exception:
            pass

    def add_warning(self, message: str) -> None:
        """Record a global concern that isn't tied to a specific element."""
        try:
            self._warnings.append(str(message))
        except Exception:
            pass

    def set_elements(self, counts: Dict[str, int]) -> None:
        """
        Set the final element counts in one call at the end of processing.
        Replaces any previous counts. Counts are the source of truth for what
        the client will receive in bim_data.
        """
        try:
            self._elements = {str(k): int(v) for k, v in counts.items()}
        except Exception:
            pass

    # ── Serialization ────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """
        Return the final report as a plain dict ready for jsonify.

        Shape:
          {
            "model_mode": "fine_tuned",
            "ocr_used": True,
            "elements": {"walls": 10, "doors": 4, ...},
            "stages":   {"ocr": {"status": "ok"}, "rooms": {"status": "ok"}, ...},
            "skipped":  [{"type": "door", "reason": "...", "id": "Door_3"}],
            "warnings": ["Input image was downsampled from 4096x4096 to 2048x2048"]
          }
        """
        try:
            return {
                "model_mode": self._model_mode,
                "ocr_used":   self._ocr_used,
                "elements":   dict(self._elements),
                "stages":     {k: dict(v) for k, v in self._stages.items()},
                "skipped":    [dict(x) for x in self._skipped],
                "warnings":   list(self._warnings),
            }
        except Exception as exc:
            logger.warning("AnalysisReport.to_dict failed (%s) — returning minimal report", exc)
            return {
                "model_mode": "unknown",
                "ocr_used":   False,
                "elements":   {},
                "stages":     {},
                "skipped":    [],
                "warnings":   ["analysis_report serialization failed"],
            }
