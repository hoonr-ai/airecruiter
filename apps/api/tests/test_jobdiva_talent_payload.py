"""Tests for the v2 TalentSearch payload: term extraction + body construction.

Context (live probe 2026-07-19, scripts/jobdiva_payload_variants_probe.py):
the v2 TalentSearch endpoint takes TalentSearchDef fields at the TOP LEVEL
of the body — `skills` is an array of plain AND'd terms, geo goes in
zipCode/withinMiles/states arrays, and boolean syntax inside a term breaks
the request. These tests pin the client-side mapping onto that contract.
"""

import asyncio
import sys
from pathlib import Path

import pytest

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from services.jobdiva_boolean_translator import (  # noqa: E402
    extract_and_terms,
    sanitize_talent_term,
)
from services.jobdiva import jobdiva_service  # noqa: E402


# ── sanitize_talent_term ──────────────────────────────────────────────────

def test_sanitize_strips_quotes_and_parens():
    assert sanitize_talent_term(' "Spring Boot" ') == "Spring Boot"
    assert sanitize_talent_term("(Java)") == "Java"


def test_sanitize_drops_operator_words_and_numbers():
    assert sanitize_talent_term("AND") == ""
    assert sanitize_talent_term("or") == ""
    assert sanitize_talent_term("25") == ""
    assert sanitize_talent_term("5+") == ""


def test_sanitize_drops_years_clauses():
    assert sanitize_talent_term("5+ years") == ""
    assert sanitize_talent_term("10 yrs") == ""
    assert sanitize_talent_term("3 years of Kafka") == ""


def test_sanitize_keeps_normal_skills():
    assert sanitize_talent_term("C++") == "C++"
    assert sanitize_talent_term("Node.js") == "Node.js"


def test_sanitize_drops_overlong_terms():
    assert sanitize_talent_term("x" * 61) == ""


# ── extract_and_terms ─────────────────────────────────────────────────────

def test_extract_top_level_and_terms():
    assert extract_and_terms('"Java" AND "Spring" AND "AWS"') == [
        "Java", "Spring", "AWS",
    ]


def test_extract_drops_not_groups():
    out = extract_and_terms('"Java" AND "Spring" NOT ("Manager" OR "Director")')
    assert out == ["Java", "Spring"]
    assert extract_and_terms('"Java" NOT "Recruiter"') == ["Java"]


def test_extract_drops_or_groups_entirely():
    # Requiring one alternative would silently exclude the others — the
    # whole OR group must be dropped, not collapsed to its first term.
    out = extract_and_terms('("Java Developer" OR "J2EE Developer") AND "Java"')
    assert out == ["Java"]


def test_extract_drops_naked_or_alternatives():
    assert extract_and_terms('"Java" OR "Python"') == []


def test_extract_strips_location_clauses():
    out = extract_and_terms('"Java" AND "Coppell, TX 75019" within 25 mi')
    assert out == ["Java"]


def test_extract_strips_years():
    assert extract_and_terms('"Databricks" AND "5+ years"') == ["Databricks"]
    assert extract_and_terms('"Python 5 years" AND "Django"') == ["Python", "Django"]


def test_extract_caps_and_dedupes():
    out = extract_and_terms(
        '"A1" AND "a1" AND "B2" AND "C3" AND "D4" AND "E5"', max_terms=4
    )
    assert out == ["A1", "B2", "C3", "D4"]


def test_extract_wizard_shaped_boolean():
    boolean = (
        '("Java Developer" OR "Backend Engineer") AND "Java" AND "Spring Boot" '
        'AND "Coppell, TX 75019" within 25 mi NOT ("Manager")'
    )
    assert extract_and_terms(boolean) == ["Java", "Spring Boot"]


# ── _search_talent_pool body construction ─────────────────────────────────

def _run_pool(monkeypatch, **kwargs):
    """Call _search_talent_pool with the network layer stubbed; capture the
    (base_body, must_terms, title) handed to the fetcher."""
    captured = {}

    async def fake_fetch(token, base_body, must_terms, title=""):
        captured["base_body"] = base_body
        captured["must_terms"] = must_terms
        captured["title"] = title
        return []

    monkeypatch.setattr(jobdiva_service, "_fetch_talent_search_rows", fake_fetch)
    defaults = dict(
        skills=[],
        location="",
        limit=150,
        token="tok",
        boolean_string="",
        require_resume=True,
        countries=["US"],
        states=[],
        zip_code="",
        within_miles=25,
        title="",
    )
    defaults.update(kwargs)
    asyncio.get_event_loop().run_until_complete(
        jobdiva_service._search_talent_pool(**defaults)
    )
    return captured


@pytest.fixture(autouse=True)
def _event_loop_guard():
    # ensure a fresh loop per test on 3.11 where get_event_loop warns
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def test_body_top_level_arrays_and_zip(monkeypatch):
    cap = _run_pool(
        monkeypatch,
        skills=[{"value": "Java", "match_type": "must"},
                {"value": "Recruiter", "match_type": "exclude"}],
        zip_code="75019",
        within_miles=25,
        title="Java Developer",
    )
    body = cap["base_body"]
    assert body["countries"] == ["US"]
    assert body["zipCode"] == "75019"
    assert body["withinMiles"] == 50  # 2x headroom
    assert "skills" not in body  # terms ride separately, per-pull
    assert cap["must_terms"] == ["Java"]  # exclude term stays client-side
    assert cap["title"] == "Java Developer"


def test_body_states_array_without_zip(monkeypatch):
    cap = _run_pool(monkeypatch, skills=["Java"], states=["tx", "OK"])
    body = cap["base_body"]
    assert body["states"] == ["TX", "OK"]
    assert "zipCode" not in body


def test_zip_skipped_for_multichip_boolean(monkeypatch):
    boolean = '"Java" AND ("Plano, TX 75024" within 25 mi OR "Austin, TX 78701" within 25 mi)'
    cap = _run_pool(monkeypatch, skills=["Java"], boolean_string=boolean, zip_code="75024")
    assert "zipCode" not in cap["base_body"]


def test_terms_fall_back_to_boolean(monkeypatch):
    cap = _run_pool(monkeypatch, skills=[], boolean_string='"Kafka" AND "Flink"')
    assert cap["must_terms"] == ["Kafka", "Flink"]


def test_radius_clamped_to_100(monkeypatch):
    cap = _run_pool(monkeypatch, skills=["Java"], zip_code="75019", within_miles=80)
    assert cap["base_body"]["withinMiles"] == 100


# ── must vs preferred chips in the server-side AND ────────────────────────

def test_preferred_chips_not_anded(monkeypatch):
    # The server ANDs every element of `skills`; a "nice to have" chip in
    # that list silently excludes candidates who lack an optional skill.
    cap = _run_pool(monkeypatch, skills=[
        {"value": "Java", "match_type": "must"},
        {"value": "Kafka", "match_type": "preferred"},
        {"value": "Kubernetes", "match_type": "nice_to_have"},
        {"value": "Recruiter", "match_type": "exclude"},
    ])
    assert cap["must_terms"] == ["Java"]


def test_preferred_only_falls_back_to_top_two(monkeypatch):
    cap = _run_pool(monkeypatch, skills=[
        {"value": "Kafka", "match_type": "preferred"},
        {"value": "Flink", "match_type": "can"},
        {"value": "Beam", "match_type": "preferred"},
    ])
    assert cap["must_terms"] == ["Kafka", "Flink"]


def test_boolean_beats_preferred_fallback(monkeypatch):
    cap = _run_pool(
        monkeypatch,
        skills=[{"value": "Kafka", "match_type": "preferred"}],
        boolean_string='"Java" AND "Spring"',
    )
    assert cap["must_terms"] == ["Java", "Spring"]


def test_plain_string_skills_still_anded(monkeypatch):
    # Legacy callers pass bare strings — treated as required, as before.
    cap = _run_pool(monkeypatch, skills=["Java", "Spring Boot"])
    assert cap["must_terms"] == ["Java", "Spring Boot"]
