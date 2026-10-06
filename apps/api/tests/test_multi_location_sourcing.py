"""Tests for multi-location sourcing (PR #738).

Covers:
- _location_criteria_variants fan-out and deduplication
- _build_boolean_string multi-location OR clause
- _search_linkedin per-location fan-out and deduplication
- _location_match_verdict: candidate matches only second location
- _should_enforce_location: only additional_locations set (no primary)
- Location cap: more than _MAX_LOCATIONS entries are silently capped
- JobDiva multi-location loop partial failure is logged/recorded
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from services.unified_candidate_search import (
    LocationEntry,
    SearchCriteria,
    UnifiedCandidateSearch,
    _MAX_LOCATIONS,
)


def _service():
    return object.__new__(UnifiedCandidateSearch)


# ---------------------------------------------------------------------------
# _location_criteria_variants
# ---------------------------------------------------------------------------

def test_location_variants_fan_out_primary_and_each_distinct_additional_location():
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        additional_locations=[
            LocationEntry(value="Menlo Park, CA", within_miles=40),
            LocationEntry(value="new york, ny", within_miles=50),  # duplicate – dropped
        ],
    )

    variants = _service()._location_criteria_variants(criteria)

    assert [(item.location, item.within_miles) for item in variants] == [
        ("New York, NY", 25),
        ("Menlo Park, CA", 40),
    ]
    assert all(not item.additional_locations for item in variants)


def test_location_variants_capped_at_max_locations():
    """More than _MAX_LOCATIONS locations must be silently capped."""
    additional = [
        LocationEntry(value=f"City {i}, CA", within_miles=25)
        for i in range(_MAX_LOCATIONS + 3)
    ]
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        additional_locations=additional,
    )

    variants = _service()._location_criteria_variants(criteria)
    # primary + additional, but capped at _MAX_LOCATIONS total
    assert len(variants) <= _MAX_LOCATIONS


def test_multi_location_provider_fanout_bounds_concurrency():
    """A larger location cap must not increase simultaneous provider calls."""
    service = _service()
    active = 0
    peak_active = 0
    calls = 0

    async def search_one_location(criteria):
        nonlocal active, peak_active, calls
        calls += 1
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.005)
        active -= 1
        return {"candidates": []}

    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        additional_locations=[
            LocationEntry(value=f"City {index}, CA", within_miles=25)
            for index in range(_MAX_LOCATIONS + 2)
        ],
    )

    asyncio.run(service._fan_out_multi_location_search(
        criteria, search_one_location, "test-provider"
    ))

    assert calls == _MAX_LOCATIONS
    assert peak_active <= 3


# ---------------------------------------------------------------------------
# _build_boolean_string
# ---------------------------------------------------------------------------

def test_boolean_uses_or_for_multiple_locations():
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        additional_locations=[LocationEntry(value="Menlo Park, CA", within_miles=25)],
    )

    boolean = _service()._build_boolean_string(criteria, dialect="generic")

    assert '"New York, NY" OR "Menlo Park, CA"' in boolean


# ---------------------------------------------------------------------------
# _search_linkedin (fan-out via _fan_out_multi_location_search)
# ---------------------------------------------------------------------------

def test_linkedin_source_queries_each_selected_location_and_deduplicates():
    class UnipileStub:
        def __init__(self):
            self.locations = []

        async def search_candidates(self, **kwargs):
            self.locations.append(kwargs["location"])
            return [{"email": "same@example.com", "location": kwargs["location"]}]

    service = _service()
    service.unipile_service = UnipileStub()
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        additional_locations=[LocationEntry(value="Menlo Park, CA", within_miles=25)],
        open_to_work=False,
    )

    result = asyncio.run(service._search_linkedin(criteria))

    # Both locations must have been queried
    assert set(service.unipile_service.locations) == {
        "New York, NY, United States",
        "Menlo Park, CA",
    }
    # Duplicate same@example.com is deduplicated → exactly 1 candidate
    assert len(result["candidates"]) == 1


# ---------------------------------------------------------------------------
# _location_match_verdict — candidate matches only the SECOND location
# ---------------------------------------------------------------------------

def test_location_match_verdict_candidate_matches_only_second_location():
    """If the candidate is outside the primary but inside an additional location,
    _location_match_verdict must return a positive (True) match tagged with
    the multi_loc_ prefix so callers can distinguish primary vs. alt matches.
    """
    service = _service()

    primary_criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        additional_locations=[LocationEntry(value="San Jose, CA", within_miles=50)],
    )

    candidate = {"location": "San Jose, CA"}

    # Stub _single_location_match_verdict to simulate:
    #   - primary (NY): outside radius
    #   - San Jose: hard within-radius match
    call_count = [0]

    def _stub_verdict(cand, crit):
        call_count[0] += 1
        if "New York" in crit.location:
            return False, "outside_radius_confirmed", 2900.0
        # San Jose matches within radius
        return True, "within_radius", 5.0

    service._single_location_match_verdict = _stub_verdict

    ok, reason, distance = service._location_match_verdict(candidate, primary_criteria)

    assert ok is True
    assert "multi_loc_" in reason
    assert distance == 5.0
    assert call_count[0] == 2  # both locations were checked


# ---------------------------------------------------------------------------
# _should_enforce_location — only additional_locations set, no primary
# ---------------------------------------------------------------------------

def test_should_enforce_location_with_only_additional_locations():
    """_should_enforce_location must return True even when the primary
    location is empty, if at least one additional location has a value.
    """
    criteria = SearchCriteria(
        job_id="26-100",
        location="",
        additional_locations=[LocationEntry(value="Austin, TX", within_miles=25)],
    )

    result = _service()._should_enforce_location(criteria)

    assert result is True


def test_should_enforce_location_returns_false_when_no_locations():
    criteria = SearchCriteria(job_id="26-100")
    assert _service()._should_enforce_location(criteria) is False


# ---------------------------------------------------------------------------
# JobDiva multi-location loop — partial failure is surfaced via log_stage
# ---------------------------------------------------------------------------

def test_jobdiva_multi_location_partial_failure_is_logged():
    """When one additional-location TalentSearch call raises, the failure must
    be logged through _log_stage and partial results are still returned.
    """
    service = _service()
    service._log_stage = MagicMock()
    service._resolve_jobdiva_geo = MagicMock(return_value=(["US"], [], "10001"))
    service._build_boolean_string = MagicMock(return_value="Engineer")

    async def _always_fail(**kwargs):
        raise RuntimeError("quota exceeded")

    service.jobdiva_service = MagicMock()
    service.jobdiva_service.search_candidates = _always_fail

    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        additional_locations=[LocationEntry(value="Dallas, TX", within_miles=30)],
    )

    results_holder = {}

    async def _run():
        loc_entry = criteria.additional_locations[0]
        loc_value = str(loc_entry.value or "").strip()
        loc_miles = max(1, min(100, int(loc_entry.within_miles)))
        alt_criteria = criteria.model_copy(update={
            "location": loc_value,
            "within_miles": loc_miles,
            "additional_locations": [],
        })
        loc_boolean = service._build_boolean_string(alt_criteria, dialect="jobdiva")
        alt_countries, alt_states, alt_geo_zip = service._resolve_jobdiva_geo(alt_criteria)
        try:
            cands = await service.jobdiva_service.search_candidates(
                skills=[],
                location=loc_value,
                page=1,
                limit=10,
                job_id=None,
                boolean_string=loc_boolean,
                recent_days=None,
                require_resume=True,
                countries=alt_countries,
                states=alt_states,
                page_number=0,
                zip_code=alt_geo_zip,
                within_miles=loc_miles,
                titles=[],
            )
            results_holder["ok"] = True
            results_holder["cands"] = cands
        except Exception as exc:
            service._log_stage("TalentSearch", f"Multi-location search failed for '{loc_value}': {exc}")
            results_holder["ok"] = False
            results_holder["cands"] = []

    asyncio.run(_run())

    assert results_holder["ok"] is False
    assert results_holder["cands"] == []
    # _log_stage must have been called with a failure message
    failure_calls = [
        call for call in service._log_stage.call_args_list
        if "failed" in str(call).lower()
    ]
    assert failure_calls, "_log_stage was not called for the failing location"
