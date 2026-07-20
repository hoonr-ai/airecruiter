"""Tests for services/company_match.py — the shared "currently employed by
the hiring client" matcher used by both sourcing filters and the launch gate."""

import sys
from pathlib import Path

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from services.company_match import (  # noqa: E402
    collect_current_companies,
    currently_employed_by_client,
    extract_company_from_headline,
    is_placeholder_client,
    is_same_company,
    normalize_company_name,
)


# ── normalization / matching ──────────────────────────────────────────────

def test_normalize_strips_legal_noise():
    assert normalize_company_name("Acme Technologies, Inc.") == "acme"
    assert normalize_company_name("Meta Platforms LLC") == "meta platforms"


def test_same_company_token_containment():
    assert is_same_company("Meta Platforms", "Meta")
    assert is_same_company("Meta", "Meta Platforms Inc")
    assert not is_same_company("Metadata Solutions", "Meta")


def test_same_company_ignores_placeholders():
    assert not is_same_company("External Corp", "External")
    assert not is_same_company("Anything", "n/a")
    assert is_placeholder_client("unknown")
    assert not is_placeholder_client("Wells Fargo")


# ── headline parsing ──────────────────────────────────────────────────────

def test_headline_at_company():
    assert extract_company_from_headline("Senior Engineer at Google") == "Google"
    assert extract_company_from_headline("PM @ Stripe") == "Stripe"


def test_headline_uses_last_at_occurrence():
    assert (
        extract_company_from_headline("Ex-Google | Data Engineer at Stripe")
        == "Stripe"
    )


def test_headline_stops_at_separators():
    assert (
        extract_company_from_headline("Engineer at Meta | ex-Amazon")
        == "Meta"
    )
    assert (
        extract_company_from_headline("Architect at Wells Fargo, Charlotte NC")
        == "Wells Fargo"
    )


def test_headline_no_match_returns_empty():
    assert extract_company_from_headline("Senior Java Developer") == ""
    assert extract_company_from_headline("") == ""


# ── current-company collection across source shapes ───────────────────────

def test_collect_from_flat_fields_and_experience():
    cand = {
        "data": {
            "current_company": "Acme Inc",
            "company_experience": [
                {"company": "OldCo", "end_date": "2020-01"},
                {"company": "NewCo", "end_date": "Present"},
            ],
        }
    }
    companies = collect_current_companies(cand)
    assert "Acme Inc" in companies
    assert "NewCo" in companies
    assert "OldCo" not in companies


def test_collect_from_exa_recent_companies():
    cand = {
        "data": {
            "exa_recent_companies": [
                {"company": "Hiring Client Co", "start": "2023-01", "end": "Present"},
                {"company": "Previous Co", "start": "2019-01", "end": "2022-12"},
            ]
        }
    }
    companies = collect_current_companies(cand)
    assert "Hiring Client Co" in companies
    assert "Previous Co" not in companies


def test_collect_from_headline_only_unipile_shape():
    cand = {"title": "Software Engineer at Wells Fargo", "data": {}}
    # top-level shape (no data wrapper) — search-row form
    cand2 = {"headline": "Software Engineer at Wells Fargo"}
    assert "Wells Fargo" in collect_current_companies(cand2)
    assert currently_employed_by_client(cand2, "Wells Fargo & Company")


def test_currently_employed_by_client_end_to_end():
    exa_row = {
        "name": "Jane Doe",
        "title": "Staff Engineer at Meta",
        "exa_recent_companies": [
            {"company": "Meta Platforms", "end": "Present"},
        ],
    }
    assert currently_employed_by_client(exa_row, "Meta")
    assert not currently_employed_by_client(exa_row, "Netflix")
    # past employment at the client must NOT exclude
    past_row = {
        "name": "Bob",
        "title": "Engineer at Stripe",
        "company_experience": [
            {"company": "Meta", "end_date": "2021-05"},
        ],
    }
    assert not currently_employed_by_client(past_row, "Meta")
