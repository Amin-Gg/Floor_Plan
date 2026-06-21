"""
Unit tests for RAG/query_router.route_query.

Pure tests — the router and the Step 1 evaluator make no API calls.
Run from the project root:  python -m pytest eval/test_query_router.py -v
"""

import pytest

from rag.query_router import route_query, VOCAB


def make_hits(scores):
    return [
        {"mabhas_part": "3", "article_id": f"a-{i}", "heading_fa": "ع",
         "text_fa": "م", "text_en": "t", "rule_type": "numeric",
         "entities": {}, "score": s}
        for i, s in enumerate(scores)
    ]


HIGH_HITS = make_hits([0.85, 0.60, 0.50, 0.45, 0.40])   # HIGH per Step 1
LOW_HITS = make_hits([0.20, 0.18, 0.15, 0.10, 0.05])    # LOW (weak top1)
MEDIUM_HITS = make_hits([0.55, 0.48, 0.45, 0.40, 0.35]) # MEDIUM


def assert_decision(d, primary, fallback, reason, rule):
    assert d["primary"] == primary
    assert d["fallback"] == fallback
    assert d["reason"] == reason
    assert d["rule"] == rule


# --- R1: already-confident --------------------------------------------------

def test_r1_high_confidence_no_transform():
    d = route_query("حداقل عرض راهرو چقدر است؟", initial_hits=HIGH_HITS)
    assert_decision(d, "none", "none", "already-confident", "R1")


def test_r1_beats_r2_keywords():
    # Exception keyword present but retrieval already HIGH -> R1 wins.
    d = route_query("موارد استثنا کدام است؟", initial_hits=HIGH_HITS)
    assert d["rule"] == "R1"


def test_r1_negative_low_hits_do_not_trigger_r1():
    d = route_query("یک پرسش معمولی درباره مقررات کلی ساختمان سازی کشور", initial_hits=LOW_HITS)
    assert d["rule"] != "R1"


def test_r1_negative_no_hits_provided():
    d = route_query("پرسش بدون بازیابی اولیه درباره مقررات کلی ملی ساختمان")
    assert d["rule"] != "R1"


# --- R2: exception-lookup -----------------------------------------------------

def test_r2_persian_exception_keyword():
    d = route_query("موارد استثنای این بند برای ساختمان های موجود چیست؟")
    assert_decision(d, "multi_query", "stepback", "exception-lookup", "R2")


def test_r2_english_exception_keyword():
    d = route_query("Which buildings are an exception to the sprinkler requirement?")
    assert d["rule"] == "R2"


def test_r2_english_doesnt_apply_phrase():
    d = route_query("When doesn't apply the fire separation requirement to warehouses?")
    assert d["rule"] == "R2"


def test_r2_negative_no_exception_vocabulary():
    d = route_query("What is the general requirement for fire separations in warehouses today?")
    assert d["rule"] != "R2"


# --- R3: short numeric query ---------------------------------------------------

def test_r3_short_persian_with_property():
    # 5 tokens (< 6) and contains عرض.
    d = route_query("حداقل عرض راهرو چقدر است؟")
    assert_decision(d, "hyde", "multi_query", "short-numeric-query", "R3")


def test_r3_short_english_with_property():
    # 4 words (< 8) and contains "height".
    d = route_query("minimum ceiling height residential?")
    assert d["rule"] == "R3"


def test_r3_negative_long_query_with_property():
    # Contains "width" but 14 words -> not short -> not R3.
    d = route_query(
        "Considering accessibility requirements and typical furniture layouts, "
        "what corridor width should architects usually provide?"
    )
    assert d["rule"] != "R3"


def test_r3_negative_short_query_without_property():
    d = route_query("قوانین کلی پارکینگ چیست؟")
    assert d["rule"] != "R3"


def test_r3_beats_r4_when_both_match():
    # 3 tokens, contains عرض, AND two element categories (door + bathroom).
    d = route_query("عرض درب حمام")
    assert d["rule"] == "R3"


# --- R4: multi-element query -----------------------------------------------------

def test_r4_persian_two_elements():
    d = route_query("آیا درب اتاق خواب می تواند مستقیما به حمام باز شود یا خیر؟")
    assert_decision(d, "multi_query", "stepback", "multi-element-query", "R4")


def test_r4_persian_zwnj_variant_matches():
    # اتاق‌خواب written with ZWNJ still counts as the bedroom category.
    d = route_query("ارتباط میان اتاق‌خواب و سرویس بهداشتی چه ضوابطی دارد در ساختمان؟")
    assert d["rule"] == "R4"


def test_r4_english_two_elements():
    d = route_query("Is it permitted to access the kitchen directly from the parking garage area?")
    assert d["rule"] == "R4"


def test_r4_negative_single_element():
    d = route_query("ضوابط کلی مربوط به فضای آشپزخانه در واحدهای مسکونی چیست؟")
    assert d["rule"] != "R4"


def test_r4_negative_two_synonyms_one_category():
    # "حمام" and "دستشویی" are the SAME category -> only 1 distinct element.
    d = route_query("ضوابط مربوط به حمام و دستشویی در واحد مسکونی چیست بگویید؟")
    assert d["rule"] != "R4"


def test_persian_preposition_dar_does_not_count_as_door():
    # Bare "در" (= "in") must not match the door category.
    d = route_query("ضوابط ایمنی در آشپزخانه های صنعتی بزرگ چیست لطفا بگویید؟")
    assert d["rule"] != "R4"  # only kitchen matched, not door


# --- R5: low-confidence fallback ----------------------------------------------

def test_r5_low_confidence_no_lexical_triggers():
    d = route_query(
        "یک پرسش کلی و طولانی درباره ضوابط عمومی طراحی که واژگان خاصی ندارد",
        initial_hits=LOW_HITS,
    )
    assert_decision(d, "hyde", "multi_query", "low-confidence-fallback", "R5")


def test_r5_negative_medium_confidence_falls_to_default():
    d = route_query(
        "یک پرسش کلی و طولانی درباره ضوابط عمومی طراحی که واژگان خاصی ندارد",
        initial_hits=MEDIUM_HITS,
    )
    assert d["rule"] == "R6"


def test_r2_beats_r5_ordering():
    # Exception keyword + LOW hits -> R2 fires first.
    d = route_query("موارد استثنای این الزام چیست؟", initial_hits=LOW_HITS)
    assert d["rule"] == "R2"


# --- R6: default ---------------------------------------------------------------

def test_r6_default_no_hits_no_triggers():
    d = route_query("توضیح کلی درباره اهداف مقررات ملی ساختمان ایران بدهید لطفا")
    assert_decision(d, "none", "hyde", "default-no-transform", "R6")


def test_r6_default_english():
    d = route_query("Please explain the general purpose of the national building regulations.")
    assert d["rule"] == "R6"


# --- Vocabulary hygiene ----------------------------------------------------------

def test_bare_dar_not_in_door_vocabulary():
    assert "در" not in VOCAB["element_types"]["door"]


def test_apostrophe_survives_normalization():
    # Regression for the contraction bug: "doesn't" must remain one token so
    # the multi-word exception phrase "doesn't apply" can match.
    from rag.query_router import _normalize
    assert "doesn't" in _normalize("Why doesn't apply this rule?")
