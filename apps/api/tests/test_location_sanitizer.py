"""Work-arrangement location sanitizer (2026-07, Job 26-23319).

A JobDiva candidate living in New York, NY rendered as "Remote" on Step 5:
the LLM extractor latched onto an employer line ("ToroNet Software
Development Company. -Remote"), the old LLM-first merges wrote it into the
row + both caches, and — because "Remote" can't geocode — the radius gate
soft-kept the row as location-unknown. These tests pin the guard that makes
work-arrangement strings unrepresentable as candidate locations, and the
merge/verdict behavior around it.

Same harness pattern as test_location_zip_verdict.py: bare instance via
object.__new__, pure methods exercised directly.
"""
import os

# Config requires these at import time; set dummies before importing anything.
for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY",
):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from services.location import sanitize_candidate_location  # noqa: E402
from services.sourced_candidates_storage import _clean_location_value  # noqa: E402
from services.unified_candidate_search import (  # noqa: E402
    UnifiedCandidateSearch,
    SearchCriteria,
)


@pytest.fixture
def svc():
    return object.__new__(UnifiedCandidateSearch)


def _criteria(**kw):
    base = dict(job_id="J1", location="New York, NY 10018", within_miles=25)
    base.update(kw)
    return SearchCriteria(**base)


# ------------------------------------------------- pure-arrangement strings

@pytest.mark.parametrize("value", [
    "Remote",
    "REMOTE",
    "remote",
    "Hybrid",
    "Onsite",
    "On-site",
    "On site",
    "WFH",
    "Work from home",
    "Work From Home",
    "Telecommute",
    "Telecommuting",
    "Virtual",
    "Anywhere",
    "100% Remote",
    "Fully Remote",
    "Remote Only",
    "Remote-first",
    "Remote OK",
    "Remote friendly",
    "Open to remote",
    "Hybrid - 3 days onsite",
    "Remote (Hybrid optional)",
    "Remote / Hybrid",
])
def test_pure_arrangement_strings_blank_out(value):
    assert sanitize_candidate_location(value) == ""


# ------------------------------------------------------ mixed strings keep place

@pytest.mark.parametrize("value,expected", [
    ("Remote - Austin, TX", "Austin, TX"),
    ("New York (Remote)", "New York"),
    ("Remote, New York, NY", "New York, NY"),
    ("Hybrid – Chicago, IL", "Chicago, IL"),
    ("REMOTE, GA", "GA"),          # CRM rows with "REMOTE" typed as the city
    ("Remote, USA", "USA"),        # country evidence survives for the country gate
])
def test_mixed_strings_keep_the_place(value, expected):
    assert sanitize_candidate_location(value) == expected


# ------------------------------------------------------------- passthrough

@pytest.mark.parametrize("value", [
    "New York, NY",
    "New York, NY 10018",
    "Tempe, AZ 85281",
    "Ajax, ON",
    "Phoenix Metropolitan Area",
    "Reading, PA",   # contains no arrangement token — must not be touched
])
def test_real_places_pass_through_unchanged(value):
    assert sanitize_candidate_location(value) == value


def test_empty_and_none_inputs():
    assert sanitize_candidate_location("") == ""
    assert sanitize_candidate_location(None) == ""
    assert sanitize_candidate_location("   ") == ""


def test_clean_location_value_rejects_arrangements_and_placeholders():
    assert _clean_location_value("Remote") is None
    assert _clean_location_value("n/a") is None
    assert _clean_location_value("") is None
    assert _clean_location_value("Remote - Austin, TX") == "Austin, TX"
    assert _clean_location_value("New York, NY") == "New York, NY"


# ------------------------------------- structured locations feed the geo gate

def test_structured_locations_drop_arrangement_only_row(svc):
    """A row whose every location signal is an arrangement string has NO
    location evidence — nothing to geocode, nothing to display."""
    assert svc._candidate_structured_locations({
        "location": "Remote",
        "enhanced_info": {"current_location": "Remote"},
    }) == []


def test_structured_locations_prefer_city_state_over_arrangement(svc):
    """Job 26-23319 regression: JobDiva record says New York, NY; the row's
    `location` was LLM-poisoned to "Remote". The structured city/state must
    win and the arrangement string must vanish."""
    locs = svc._candidate_structured_locations({
        "city": "New York",
        "state": "NY",
        "location": "Remote",
        "enhanced_info": {"current_location": "Remote"},
    })
    assert locs == ["New York, NY"]


def test_structured_locations_poisoned_cache_cannot_fill_blank(svc):
    """Thin source row + cached LLM "Remote": the blank-fill consults the
    LLM value but the sanitizer blanks it — no location evidence invented."""
    assert svc._candidate_structured_locations({
        "location": "",
        "enhanced_info": {"current_location": "Remote"},
    }) == []


def test_structured_locations_llm_place_still_fills_blank(svc):
    """A real LLM-extracted place still works as the last-resort fallback."""
    assert svc._candidate_structured_locations({
        "location": "",
        "enhanced_info": {"current_location": "Phoenix, AZ"},
    }) == ["Phoenix, AZ"]


# ---------------------------------------------------------- radius verdict

def test_verdict_arrangement_only_candidate_is_location_unknown(svc, monkeypatch):
    """"Remote" used to reach the geocoder as if it were a place. Now the row
    is honestly location-unknown: soft-kept with the sentinel distance (UI
    counts it under BEYOND-radius) and no geocode attempt."""
    import services.unified_candidate_search as ucs

    def boom(*a, **kw):
        raise AssertionError("must not geocode a work-arrangement string")

    monkeypatch.setattr(ucs, "within_radius", boom)
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Remote", "enhanced_info": {"current_location": "Remote"}},
        _criteria(),
    )
    assert ok and reason == "candidate_location_missing_keep"
    assert dist is not None  # sentinel distance, not "in radius"


def test_verdict_real_location_beats_poisoned_location_string(svc):
    """New York candidate whose `location` says "Remote" but whose structured
    city/state is real: the verdict is computed from New York, NY — for a
    New York job that's a confirmed in-radius match, not unknown."""
    ok, reason, dist = svc._location_match_verdict(
        {
            "city": "New York",
            "state": "NY",
            "location": "Remote",
            "enhanced_info": {"current_location": "Remote"},
        },
        _criteria(),
    )
    assert ok
    assert reason in ("city_state_match", "within_radius", "within_radius_offline")
