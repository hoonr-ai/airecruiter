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
    _UNKNOWN_DISTANCE_SENTINEL,
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
# Exa sources: variants that search the same location string run once
# ---------------------------------------------------------------------------

class _ExaStub:
    """Records the location each Exa people search / Dice search was asked for."""

    def __init__(self):
        self.locations = []
        self.dice_locations = []

    async def search_candidates(self, **kwargs):
        self.locations.append(kwargs["location"])
        return [{"id": "exa_linkedin.com/in/same", "profile_url": "https://www.linkedin.com/in/same",
                 "source": "LinkedIn-Exa"}]

    async def search_dice_candidates(self, **kwargs):
        self.dice_locations.append(kwargs["location"])
        return [{"id": "dice_0_x", "profile_url": "https://www.dice.com/x", "source": "Dice"}]


def _remote_criteria(**kw):
    return SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        location_type="Remote",
        additional_locations=[
            LocationEntry(value="Austin, TX", within_miles=25),
            LocationEntry(value="Denver, CO", within_miles=25),
        ],
        open_to_work=False,
        **kw,
    )


def test_remote_multi_location_dedupes_exa_variants():
    """A Remote job searches its country whatever the location, so its three
    locations used to buy the same Exa searches three times."""
    service = _service()
    service.exa_service = _ExaStub()

    result = asyncio.run(service._search_exa(_remote_criteria()))

    assert service.exa_service.locations == ["United States"]
    assert [c["id"] for c in result["candidates"]] == ["exa_linkedin.com/in/same"]
    assert result["source_type"] == "LinkedIn-Exa"


def test_remote_multi_location_dedupes_dice_variants():
    service = _service()
    service.exa_service = _ExaStub()

    asyncio.run(service._search_dice(_remote_criteria()))

    assert service.exa_service.dice_locations == ["United States"]


def test_onsite_distinct_locations_still_search_each_for_exa():
    service = _service()
    service.exa_service = _ExaStub()
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        additional_locations=[LocationEntry(value="Menlo Park, CA", within_miles=25)],
        open_to_work=False,
    )

    asyncio.run(service._search_exa(criteria))

    assert sorted(service.exa_service.locations) == ["Menlo Park, CA", "New York, NY, United States"]


def test_resolve_bare_zip_for_external_source_returns_city_state():
    """A bare ZIP carries no signal once it reaches Exa/Dice: their query
    builder strips every digit out of the location text
    (exa_service._strip_zip_for_query — LinkedIn profile location lines
    never show a zip), so "07310, United States" degenerated to just
    "United States" and the search silently lost its location entirely.
    Resolving the zip to "City, ST" first — the same backfill JobDiva's own
    geo resolution already does (_resolve_jobdiva_geo) — keeps a real place
    name in the text the whole way through."""
    service = _service()
    # Stubbed rather than relying on the bundled zip dataset's real content
    # for 07310/00000 — hermetic to dataset changes, same shape lookup_zip
    # actually returns ({"zip", "city", "state", "lat", "lng"} or None).
    fake_entry = {"zip": "07310", "city": "Jersey City", "state": "NJ", "lat": 40.0, "lng": -74.0}
    with patch("services.zip_index.lookup_zip", side_effect=lambda z: fake_entry if z == "07310" else None):
        assert service._resolve_bare_zip_for_external_source("07310") == "Jersey City, NJ"
        # A city/state string, or anything that isn't a bare 5-digit zip,
        # must pass through unchanged — this only targets the bare-zip case.
        assert service._resolve_bare_zip_for_external_source("New York, NY") == "New York, NY"
        assert service._resolve_bare_zip_for_external_source("") == ""
        assert service._resolve_bare_zip_for_external_source("00000") == "00000"  # unknown zip


def test_onsite_bare_zip_location_resolves_to_city_state_for_exa():
    """End-to-end regression for the reported "multiple ZIP codes not
    working in Exa sourcing" bug: a bare-ZIP additional location must reach
    Exa as "City, ST, United States", not "07310, United States" (which
    exa_service.compose_people_query's zip-stripping would otherwise
    collapse to the generic, location-less "based in the United States")."""
    service = _service()
    service.exa_service = _ExaStub()
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        additional_locations=[LocationEntry(value="07310", within_miles=25)],
        open_to_work=False,
    )
    fake_entry = {"zip": "07310", "city": "Jersey City", "state": "NJ", "lat": 40.0, "lng": -74.0}

    with patch("services.zip_index.lookup_zip", side_effect=lambda z: fake_entry if z == "07310" else None):
        asyncio.run(service._search_exa(criteria))

    assert sorted(service.exa_service.locations) == [
        "Jersey City, NJ, United States",
        "New York, NY, United States",
    ]


def test_unipile_remote_variants_are_not_deduped():
    """Only the Exa sources pass a dedupe key (Unipile's radius and query
    differ per location)."""
    class UnipileStub:
        def __init__(self):
            self.locations = []

        async def search_candidates(self, **kwargs):
            self.locations.append(kwargs["location"])
            return []

    service = _service()
    service.unipile_service = UnipileStub()

    asyncio.run(service._search_linkedin(_remote_criteria()))

    assert service.unipile_service.locations == ["United States"] * 3


def test_a_dedupe_key_that_fails_keeps_the_variant():
    service = _service()
    seen = []

    async def single(variant):
        seen.append(variant.location)
        return {"candidates": []}

    def key(variant):
        if variant.location == "Austin, TX":
            raise ValueError("unparseable")
        return "United States"

    asyncio.run(service._fan_out_multi_location_search(
        _remote_criteria(), single, "LinkedIn-Exa", dedupe_key=key,
    ))

    assert seen == ["New York, NY", "Austin, TX"]


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


def test_jobdiva_additional_location_boolean_is_scoped_to_just_that_location():
    """Regression: every additional-location TalentSearch call used to reuse
    criteria.boolean_string — the wizard's always-non-empty, all-locations-
    OR'd string — instead of a boolean scoped to just that location. This
    test calls the real _search_jobdiva_talent_search (not a reimplemented
    loop), unlike test_jobdiva_multi_location_partial_failure_is_logged above,
    which stubs _build_boolean_string and so never exercises this line.

    jobdiva.py's count_location_clauses() gate sees 2+ `"..." within N mi`
    clauses in a boolean and skips the precise zip+radius anchor for that
    call, on the (correct, for the ONE combined-primary call) theory that a
    multi-chip search can't be pinned to one chip's zip. Reusing the combined
    boolean for every additional-location call — each already scoped to
    exactly one location — triggered that same skip for all of them,
    silently falling back to a state-wide filter. A named "City, ST" entry
    still returned plausible-looking results from that fallback; a bare-ZIP
    entry (same bug) visibly never got its configured radius.
    """
    service = _service()
    calls = []

    async def _fake_search(**kwargs):
        calls.append(kwargs)
        return []

    service.jobdiva_service = MagicMock()
    service.jobdiva_service.search_candidates = _fake_search
    service._resolve_jobdiva_geo = MagicMock(
        side_effect=lambda c: (["US"], [], "10001" if c.location == "New York, NY" else "07310")
    )

    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        # Exactly what the Step 5 wizard always sends: every location OR'd
        # into one combined text clause (see search_terms.py:429-446) — this
        # must never leak into an additional-location call's own boolean.
        boolean_string='("New York, NY" within 25 mi OR "07310" within 50 mi)',
        additional_locations=[LocationEntry(value="07310", within_miles=50)],
    )

    asyncio.run(service._search_jobdiva_talent_search(criteria))

    assert len(calls) == 2
    primary_call, additional_call = calls
    assert "New York, NY" not in additional_call["boolean_string"]

    from services.jobdiva_boolean_translator import count_location_clauses
    assert count_location_clauses(additional_call["boolean_string"]) < 2
    # The primary call is unaffected — it's the ONE call meant to carry the
    # full combined text, per search_terms.py's own documented design.
    assert primary_call["boolean_string"] == criteria.boolean_string


def test_jobdiva_additional_location_keeps_a_hand_edited_boolean_skill_term():
    """PR review follow-up: the fix above must not rebuild the additional-
    location boolean from alt_criteria's skills/titles from scratch — that
    would silently discard a term the recruiter hand-added to the wizard's
    boolean text that isn't reflected in criteria.skill_criteria. Stripping
    just the location OR group out of criteria.boolean_string (instead of
    rebuilding) keeps every other hand-edit intact."""
    service = _service()
    calls = []

    async def _fake_search(**kwargs):
        calls.append(kwargs)
        return []

    service.jobdiva_service = MagicMock()
    service.jobdiva_service.search_candidates = _fake_search
    service._resolve_jobdiva_geo = MagicMock(return_value=(["US"], [], ""))

    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        # No "Rust" in skill_criteria — a term the recruiter typed directly
        # into the wizard's boolean box, which _build_boolean_string(alt_
        # criteria, ...) would have no way to reconstruct from scratch.
        boolean_string='"Python" AND "Rust" AND ("New York, NY" within 25 mi OR "07310" within 50 mi)',
        additional_locations=[LocationEntry(value="07310", within_miles=50)],
    )

    asyncio.run(service._search_jobdiva_talent_search(criteria))

    assert len(calls) == 2
    _primary_call, additional_call = calls
    assert '"Rust"' in additional_call["boolean_string"]
    assert '"Python"' in additional_call["boolean_string"]
    assert "New York, NY" not in additional_call["boolean_string"]


# ---------------------------------------------------------------------------
# _location_match_verdict — verdict precedence ordering (PR #763)
#
# Covers the four cases requested in Akarsh's review:
#   1. hard-drop then soft-keep  → soft-keep wins
#   2. soft-keep then hard-drop  → soft-keep wins
#   3. two hard-drops            → closest distance wins
#   4. soft-keep then in-radius  → in-radius wins (returned immediately)
# ---------------------------------------------------------------------------

def _make_criteria(*additional_locs):
    """Build a two-or-more-location SearchCriteria for verdict tests."""
    return SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        additional_locations=list(additional_locs),
    )


def _stub_verdicts(*sequence):
    """Return a _single_location_match_verdict stub that replays a fixed
    sequence of (ok, reason, distance) tuples in call order."""
    iterator = iter(sequence)

    def _stub(cand, crit):
        return next(iterator)

    return _stub


def test_verdict_hard_drop_then_soft_keep_soft_keep_wins():
    """Primary: hard-drop at 2900 mi. Additional: soft-keep (geocode_unavailable).
    The soft-keep must win — the candidate may well be local."""
    service = _service()
    service._single_location_match_verdict = _stub_verdicts(
        (False, "outside_radius_confirmed", 2900.0),  # primary → hard-drop
        (False, "geocode_unavailable", _UNKNOWN_DISTANCE_SENTINEL),  # alt → soft-keep
    )

    criteria = _make_criteria(LocationEntry(value="Seattle, WA", within_miles=25))
    ok, reason, distance = service._location_match_verdict({}, criteria)

    assert ok is False
    assert reason == "geocode_unavailable"
    assert distance == _UNKNOWN_DISTANCE_SENTINEL


def test_verdict_soft_keep_then_hard_drop_soft_keep_wins():
    """Primary: soft-keep. Additional: confirmed hard-drop at 3000 mi.
    The hard-drop must NOT overwrite the soft-keep regardless of order."""
    service = _service()
    service._single_location_match_verdict = _stub_verdicts(
        (False, "geocode_unavailable", _UNKNOWN_DISTANCE_SENTINEL),  # primary → soft-keep
        (False, "outside_radius_confirmed", 3000.0),  # alt → hard-drop
    )

    criteria = _make_criteria(LocationEntry(value="Dallas, TX", within_miles=25))
    ok, reason, distance = service._location_match_verdict({}, criteria)

    assert ok is False
    assert reason == "geocode_unavailable"
    assert distance == _UNKNOWN_DISTANCE_SENTINEL


def test_verdict_two_hard_drops_closest_wins():
    """Primary: outside at 2900 mi. Additional: outside at 200 mi.
    Both are hard-drops; the shorter distance (200 mi) must be preferred."""
    service = _service()
    service._single_location_match_verdict = _stub_verdicts(
        (False, "outside_radius_confirmed", 2900.0),  # primary
        (False, "outside_radius_confirmed", 200.0),   # additional — closer
    )

    criteria = _make_criteria(LocationEntry(value="Chicago, IL", within_miles=25))
    ok, reason, distance = service._location_match_verdict({}, criteria)

    assert ok is False
    assert reason == "outside_radius_confirmed"
    assert distance == 200.0


def test_verdict_soft_keep_then_in_radius_in_radius_wins():
    """Primary: soft-keep. Additional: confirmed within-radius at 8 mi.
    in-radius is returned immediately with the multi_loc_ prefix."""
    service = _service()
    service._single_location_match_verdict = _stub_verdicts(
        (False, "geocode_unavailable", _UNKNOWN_DISTANCE_SENTINEL),  # primary → soft-keep
        (True, "within_radius", 8.0),  # additional → in-radius
    )

    criteria = _make_criteria(LocationEntry(value="Bellevue, WA", within_miles=25))
    ok, reason, distance = service._location_match_verdict({}, criteria)

    assert ok is True
    assert "multi_loc_" in reason
    assert distance == 8.0

