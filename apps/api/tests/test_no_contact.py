"""Tests for services/no_contact.py — the no-contact company list matcher.

Candidates currently or last employed by a listed company are shown in Step 5
but greyed out: never LLM-scored, never persisted, no actions.
"""

import sys
from pathlib import Path

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from services.no_contact import (  # noqa: E402
    apply_no_contact_flag,
    check_no_contact,
    collect_last_companies,
    get_no_contact_companies,
    matches_no_contact_company,
)

KW = ["Kaiser", "Citibank", "Intuit"]


# ── keyword matching (loose fuzzy, bounded) ───────────────────────────────

def test_default_list_is_kaiser_citibank_intuit():
    assert get_no_contact_companies() == ["Kaiser", "Citibank", "Intuit"]


def test_exact_and_containment_matches():
    assert matches_no_contact_company("Kaiser", KW) == "Kaiser"
    assert matches_no_contact_company("Kaiser Permanente", KW) == "Kaiser"
    assert matches_no_contact_company("KAISER PERMANENTE Northern CA", KW) == "Kaiser"
    assert matches_no_contact_company("Citibank N.A.", KW) == "Citibank"
    assert matches_no_contact_company("Intuit Inc.", KW) == "Intuit"


def test_split_and_typo_spellings_match():
    # collapsed n-gram equality
    assert matches_no_contact_company("Citi Bank", KW) == "Citibank"
    # one-typo variants (Damerau-Levenshtein ≤ 1)
    assert matches_no_contact_company("Citybank", KW) == "Citibank"
    assert matches_no_contact_company("Kasier Permanente", KW) == "Kaiser"


def test_lookalike_companies_do_not_match():
    assert matches_no_contact_company("Intuitive Surgical", KW) is None
    assert matches_no_contact_company("Citizens Bank", KW) is None
    assert matches_no_contact_company("Citigroup", KW) is None
    assert matches_no_contact_company("Kaiserhoff Industries", KW) is None
    assert matches_no_contact_company("", KW) is None
    assert matches_no_contact_company("Google", KW) is None


def test_short_keywords_never_fuzzy_match():
    # A future short keyword must not fuzzy-explode ("GE" vs "GM").
    assert matches_no_contact_company("GM", ["GE"]) is None
    assert matches_no_contact_company("GE", ["GE"]) == "GE"


# ── current vs last employer ──────────────────────────────────────────────

def test_current_employer_flat_field():
    hit = check_no_contact({"current_company": "Kaiser Permanente"}, KW)
    assert hit == {
        "company": "Kaiser Permanente",
        "keyword": "Kaiser",
        "relation": "current",
    }


def test_current_employer_from_headline():
    hit = check_no_contact({"headline": "Senior Engineer at Citibank"}, KW)
    assert hit is not None and hit["relation"] == "current"
    assert hit["keyword"] == "Citibank"


def test_last_employer_matches():
    cand = {
        "current_company": "Google",
        "company_experience": [
            {"company": "Google", "is_current": True},
            {"company": "Intuit", "end_date": "2024-05"},
            {"company": "Kaiser Permanente", "end_date": "2019-01"},
        ],
    }
    hit = check_no_contact(cand, KW)
    assert hit == {"company": "Intuit", "keyword": "Intuit", "relation": "last"}


def test_older_than_last_employer_does_not_flag():
    cand = {
        "current_company": "Google",
        "company_experience": [
            {"company": "Google", "is_current": True},
            {"company": "Microsoft", "end_date": "May 2024"},
            {"company": "Kaiser Permanente", "end_date": "2019"},
        ],
    }
    assert collect_last_companies(cand) == ["Microsoft"]
    assert check_no_contact(cand, KW) is None


def test_undated_history_falls_back_to_list_order():
    cand = {
        "company_experience": [
            {"company": "Stripe", "is_current": True},
            {"company": "Citibank"},   # end date missing but is not current
            {"company": "Kaiser"},
        ]
    }
    # Reverse-chronological assumption: first past entry is the last employer.
    # (entries with no end date read as current in _entry_is_current, so give
    # them explicit non-current markers)
    cand["company_experience"][1]["current"] = False
    cand["company_experience"][1]["end_date"] = "unknown"
    cand["company_experience"][2]["end_date"] = "unknown"
    assert collect_last_companies(cand) == ["Citibank"]


def test_nested_data_and_enhanced_info_shapes():
    cand = {
        "data": {
            "enhanced_info": {
                "company_experience": [
                    {"company": "Acme", "is_current": True},
                    {"company": "Citibank NA", "end_date": "2025-11"},
                ]
            }
        }
    }
    hit = check_no_contact(cand, KW)
    assert hit is not None and hit["relation"] == "last"


# ── flag stamping ─────────────────────────────────────────────────────────

def test_apply_flag_stamps_fields():
    cand = {"name": "Jane", "current_company": "Kaiser Permanente"}
    assert apply_no_contact_flag(cand) is True
    assert cand["no_contact"] is True
    assert cand["no_contact_company"] == "Kaiser"
    assert "Kaiser Permanente" in cand["no_contact_reason"]


def test_apply_flag_clears_stale_flag():
    cand = {
        "name": "Jane",
        "current_company": "Google",
        "no_contact": True,
        "no_contact_reason": "stale",
        "no_contact_company": "Kaiser",
    }
    assert apply_no_contact_flag(cand) is False
    assert cand["no_contact"] is False
    assert "no_contact_reason" not in cand
    assert "no_contact_company" not in cand


def test_clean_candidate_not_flagged():
    cand = {"name": "Bob", "current_company": "Netflix", "company_experience": []}
    assert apply_no_contact_flag(cand) is False
    assert cand.get("no_contact") in (None, False)
