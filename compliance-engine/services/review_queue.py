"""
services/review_queue.py
========================
Step 12 — Human Review Queue

The compliance engine deliberately flags every rule it cannot deterministically
verify as NEEDS_REVIEW (≈306 of 324 on the full corpus, until real plans narrow
that down). Those items need a qualified professional to make the final call.
This module manages that workflow.

What it does
------------
* Turns the NEEDS_REVIEW findings of a compliance result into pending review
  items, each with a stable id.
* Lets a reviewer record a final decision (PASS / FAIL / keep-as-review) with a
  note and their identity.
* Persists every decision to a JSON store (survives restarts).
* Produces the *resolved* findings + summary — the original result with human
  decisions merged in — which is what the final report should reflect.
* Surfaces analytics: which clauses get reviewed most and how they are usually
  decided. This is the SAFE form of "feeding back to the rule registry": it
  tells you which clauses are good candidates to make deterministic (add to
  OBJECT_MAP / a relation handler), rather than silently auto-applying one
  plan's human verdict to another plan.

Safety principle
----------------
A human decision applies ONLY to the specific item it was made on (this plan,
this element, this clause). The system NEVER reuses a decision across plans —
that would risk propagating a wrong call. Cross-plan learning is surfaced as a
suggestion for a developer to review, never as an automatic verdict.

No LLM, no web framework dependency — a plain class with a JSON store, so it is
testable standalone and easy to wire into the Step 10 FastAPI service.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from numeric_checker import Verdict

# Decisions a reviewer may record.
_ALLOWED_DECISIONS = {Verdict.PASS.value, Verdict.FAIL.value, Verdict.NEEDS_REVIEW.value}

# How many times a clause must be decided the SAME way before it is suggested as
# a candidate to make deterministic. Editable.
SUGGESTION_THRESHOLD = 3


class ReviewQueue:
    """
    Manages pending review items and recorded decisions, backed by a JSON file.

    Usage
    -----
        q = ReviewQueue("review_store.json")
        q.enqueue_result(result.to_dict(), plan_id="Plan_04")
        for item in q.pending(plan_id="Plan_04"):
            ...                                    # show to reviewer
        q.decide(item_id, "PASS", reviewer="eng_ahmadi", note="window faces 8m street")
        final = q.resolved_summary("Plan_04")      # summary with human calls merged
    """

    def __init__(self, store_path: str = "review_store.json"):
        self.store_path = store_path
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if os.path.exists(self.store_path):
            try:
                with open(self.store_path, encoding="utf-8") as f:
                    self._items = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._items = {}

    def _save(self) -> None:
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.store_path)   # atomic write

    # ── ingest ───────────────────────────────────────────────────────────────

    def enqueue_result(self, result_dict: Dict[str, Any], plan_id: str) -> List[Dict[str, Any]]:
        """
        Create pending review items from the NEEDS_REVIEW findings of a result.
        Idempotent per (plan_id, article_id, element_id): re-enqueuing the same
        plan won't duplicate items, and already-decided items keep their decision.
        Returns the list of pending items for this plan.
        """
        created = []
        with self._lock:
            for f in result_dict.get("findings", []):
                if f.get("verdict") != Verdict.NEEDS_REVIEW.value:
                    continue
                sig = self._signature(plan_id, f)
                if sig in self._items:
                    continue   # already tracked (possibly already decided)
                item = {
                    "item_id":          uuid.uuid4().hex[:12],
                    "signature":        sig,
                    "plan_id":          plan_id,
                    "article_id":       f.get("article_id"),
                    "element_id":       f.get("element_id"),
                    "object":           f.get("object"),
                    "message":          f.get("message"),
                    "rule_text_en":     f.get("rule_text_en"),
                    "original_verdict": Verdict.NEEDS_REVIEW.value,
                    "status":           "pending",
                    "reviewer_verdict": None,
                    "reviewer":         None,
                    "note":             None,
                    "created_at":       datetime.now().isoformat(),
                    "decided_at":       None,
                }
                self._items[sig] = item
                created.append(item)
            self._save()
        return self.pending(plan_id)

    # ── reviewer actions ──────────────────────────────────────────────────────

    def decide(self, item_id: str, verdict: str, reviewer: str,
               note: str = "") -> Dict[str, Any]:
        """Record a reviewer's final decision on one item."""
        if verdict not in _ALLOWED_DECISIONS:
            raise ValueError(f"verdict must be one of {sorted(_ALLOWED_DECISIONS)}")
        with self._lock:
            item = self._find_by_id(item_id)
            if item is None:
                raise KeyError(f"No review item with id {item_id}")
            item["status"] = "decided" if verdict != Verdict.NEEDS_REVIEW.value else "deferred"
            item["reviewer_verdict"] = verdict
            item["reviewer"] = reviewer
            item["note"] = note
            item["decided_at"] = datetime.now().isoformat()
            self._save()
            return dict(item)

    # ── queries ────────────────────────────────────────────────────────────────

    def pending(self, plan_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [dict(i) for i in self._items.values()
                if i["status"] in ("pending", "deferred")
                and (plan_id is None or i["plan_id"] == plan_id)]

    def decided(self, plan_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [dict(i) for i in self._items.values()
                if i["status"] == "decided"
                and (plan_id is None or i["plan_id"] == plan_id)]

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        item = self._find_by_id(item_id)
        return dict(item) if item else None

    # ── merge decisions back into a result ─────────────────────────────────────

    def resolved_findings(self, result_dict: Dict[str, Any],
                          plan_id: str) -> List[Dict[str, Any]]:
        """
        Return the result's findings with human decisions applied. A finding that
        a reviewer decided is updated to the reviewer's verdict and annotated with
        the reviewer + note. Findings with no decision are left unchanged.
        """
        decisions = {i["signature"]: i for i in self._items.values()
                     if i["plan_id"] == plan_id and i["status"] == "decided"}
        out = []
        for f in result_dict.get("findings", []):
            f = dict(f)
            if f.get("verdict") == Verdict.NEEDS_REVIEW.value:
                sig = self._signature(plan_id, f)
                d = decisions.get(sig)
                if d:
                    f["verdict"] = d["reviewer_verdict"]
                    note = f" [reviewed by {d['reviewer']}"
                    note += f": {d['note']}]" if d.get("note") else "]"
                    f["message"] = (f.get("message") or "") + note
            out.append(f)
        return out

    def resolved_summary(self, result_dict: Dict[str, Any],
                        plan_id: str) -> Dict[str, int]:
        """Recompute PASS/FAIL/NEEDS_REVIEW after merging human decisions."""
        out = {"PASS": 0, "FAIL": 0, "NEEDS_REVIEW": 0}
        for f in self.resolved_findings(result_dict, plan_id):
            v = f.get("verdict", Verdict.NEEDS_REVIEW.value)
            out[v] = out.get(v, 0) + 1
        return out

    # ── analytics: safe feedback to the rule registry ──────────────────────────

    def suggestions(self) -> List[Dict[str, Any]]:
        """
        Surface clauses that are repeatedly reviewed and decided the SAME way,
        as candidates to make deterministic. This is a developer hint only — it
        never changes a verdict automatically.
        """
        # group decided items by (article_id, decision)
        tally: Dict[str, Counter] = defaultdict(Counter)
        for i in self._items.values():
            if i["status"] == "decided":
                tally[i["article_id"]][i["reviewer_verdict"]] += 1

        out = []
        for article_id, counter in tally.items():
            verdict, n = counter.most_common(1)[0]
            total = sum(counter.values())
            # only suggest when consistent and frequent enough
            if n >= SUGGESTION_THRESHOLD and n == total:
                out.append({
                    "article_id": article_id,
                    "consistent_verdict": verdict,
                    "times_decided": n,
                    "hint": (f"Clause {article_id} has been decided {verdict} "
                             f"{n} times with no disagreement — consider encoding "
                             f"it as a deterministic rule."),
                })
        return sorted(out, key=lambda x: -x["times_decided"])

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _signature(plan_id: str, finding: Dict[str, Any]) -> str:
        """Stable key for an item: one per (plan, clause, element)."""
        return f"{plan_id}|{finding.get('article_id')}|{finding.get('element_id')}"

    def _find_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        for i in self._items.values():
            if i["item_id"] == item_id:
                return i
        return None
