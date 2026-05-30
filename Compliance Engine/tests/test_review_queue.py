"""tests/test_review_queue.py — Step 12 smoke tests."""
import sys, os, json, tempfile
sys.path.insert(0, ".")
from review_queue import ReviewQueue, SUGGESTION_THRESHOLD

# A synthetic compliance result with mixed verdicts
def make_result():
    return {
        "summary": {"PASS": 1, "FAIL": 1, "NEEDS_REVIEW": 3},
        "findings": [
            {"article_id":"4-3-2-1","verdict":"PASS","message":"Room_01 area ok","element_id":"Room_01","object":"bedroom","rule_text_en":"min area"},
            {"article_id":"4-5-1-3","verdict":"FAIL","message":"kitchen↔bath","element_id":"Rk↔Rb","object":"spatial","rule_text_en":"no direct door"},
            {"article_id":"8-4-4-4","verdict":"NEEDS_REVIEW","message":"window faces open space?","element_id":"Room_01","object":"glass","rule_text_en":"natural light"},
            {"article_id":"4-6-1","verdict":"NEEDS_REVIEW","message":"ceiling height?","element_id":"Room_02","object":"height","rule_text_en":"min height"},
            {"article_id":"4-9-2","verdict":"NEEDS_REVIEW","message":"ventilation?","element_id":"Room_03","object":"vent","rule_text_en":"ventilation"},
        ],
    }

def run():
    tmp = tempfile.mkdtemp()
    store = os.path.join(tmp, "review.json")
    q = ReviewQueue(store)
    res = make_result()

    # 1. enqueue → only the 3 NEEDS_REVIEW become items
    pending = q.enqueue_result(res, plan_id="Plan_04")
    assert len(pending) == 3, f"expected 3 pending, got {len(pending)}"
    print(f"PASS: enqueued 3 review items (PASS/FAIL excluded)")

    # 2. idempotent — re-enqueue doesn't duplicate
    q.enqueue_result(res, plan_id="Plan_04")
    assert len(q.pending("Plan_04")) == 3
    print("PASS: re-enqueue is idempotent (no duplicates)")

    # 3. decide one item → PASS
    item = pending[0]
    decided = q.decide(item["item_id"], "PASS", reviewer="eng_ahmadi", note="faces 8m street")
    assert decided["status"] == "decided" and decided["reviewer_verdict"] == "PASS"
    assert len(q.pending("Plan_04")) == 2
    print("PASS: decided one item → 2 pending remain")

    # 4. resolved findings reflect the human decision
    resolved = q.resolved_findings(res, "Plan_04")
    decided_finding = next(f for f in resolved if f["article_id"]==item["article_id"])
    assert decided_finding["verdict"] == "PASS"
    assert "reviewed by eng_ahmadi" in decided_finding["message"]
    print("PASS: resolved findings show reviewer verdict + note")

    # 5. resolved summary recomputed (1 review→PASS, so PASS:2 FAIL:1 REVIEW:2)
    summ = q.resolved_summary(res, "Plan_04")
    assert summ["PASS"] == 2 and summ["FAIL"] == 1 and summ["NEEDS_REVIEW"] == 2, summ
    print(f"PASS: resolved summary recomputed → {summ}")

    # 6. invalid verdict rejected
    try:
        q.decide(item["item_id"], "MAYBE", reviewer="x")
        assert False, "should reject invalid verdict"
    except ValueError:
        print("PASS: invalid verdict rejected")

    # 7. persistence — reload from disk keeps decisions
    q2 = ReviewQueue(store)
    assert len(q2.pending("Plan_04")) == 2
    assert len(q2.decided("Plan_04")) == 1
    print("PASS: decisions persist across reload")

    # 8. suggestions — decide the SAME clause the same way across 3 plans
    qd = ReviewQueue(os.path.join(tmp, "review2.json"))
    for i, plan in enumerate(["P1","P2","P3"]):
        r = {"findings":[{"article_id":"8-4-4-4","verdict":"NEEDS_REVIEW",
              "message":"site","element_id":f"R{i}","object":"glass","rule_text_en":"light"}]}
        qd.enqueue_result(r, plan_id=plan)
        it = qd.pending(plan)[0]
        qd.decide(it["item_id"], "PASS", reviewer="eng", note="ok")
    sugg = qd.suggestions()
    assert any(s["article_id"]=="8-4-4-4" and s["times_decided"]>=SUGGESTION_THRESHOLD for s in sugg)
    print(f"PASS: suggestions surfaced clause decided {SUGGESTION_THRESHOLD}x consistently")

    # 9. cross-plan safety — a decision on Plan_04 does NOT auto-resolve Plan_05
    res5 = make_result()
    q.enqueue_result(res5, plan_id="Plan_05")
    # Plan_05's same clause is still pending (not auto-passed from Plan_04)
    p5 = q.pending("Plan_05")
    assert any(i["article_id"]==item["article_id"] for i in p5), \
        "Plan_05 item should still be pending — no cross-plan auto-apply"
    print("PASS: decision did NOT leak across plans (safety)")

    print("\n=== ALL TESTS PASSED ===")

if __name__ == "__main__":
    run()
