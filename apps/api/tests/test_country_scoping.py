"""Country-aware sourcing & location-precedence tests (2026-07-30).

Covers the Job 26-22448 bug class:
  1. `_resolve_jobdiva_geo` "CA" collision — "Los Angeles, CA" used to scope
     the JobDiva TalentSearch to country=Canada, and "Toronto, ON" fell back
     to a US-wide search with no state.
  2. `_target_country` / `_scope_location_to_country` — Canadian jobs used to
     hand Exa/Unipile "Toronto, ON, United States".
  3. `_is_likely_outside_country` — the US-only gate used to drop
     "Toronto, Ontario, Canada" candidates for a Toronto job.
  4. Location precedence — the LLM-extracted `enhanced_info.current_location`
     ("Hyderabad, India" from resume body text) used to outrank the
     source-native JobDiva record ("Ajax, ON") in structured-location
     signals.
  5. `_location_hard_gate` now stamps a machine-readable
     `location_veto_reason` used by the external-source drop gate.

Same harness pattern as test_location_zip_verdict.py: bare instance via
object.__new__, pure methods only.
"""
import pytest  # noqa: E402

from services.unified_candidate_search import (  # noqa: E402
    UnifiedCandidateSearch,
    SearchCriteria,
)


@pytest.fixture
def svc():
    return object.__new__(UnifiedCandidateSearch)


def _criteria(**kw):
    base = dict(job_id="J1", location="Tempe, AZ 85281", within_miles=25)
    base.update(kw)
    return SearchCriteria(**base)


# ------------------------------------------------- _resolve_jobdiva_geo

def test_california_is_us_not_canada(svc):
    """"CA" in the state slot is California — the old right-to-left country
    walk matched CA-the-country and scoped every California job to Canada."""
    countries, states, _zip = svc._resolve_jobdiva_geo(
        _criteria(location="Los Angeles, CA")
    )
    assert countries == ["US"]
    assert states == ["CA"]


def test_toronto_scopes_to_canada_with_province(svc):
    countries, states, zip_code = svc._resolve_jobdiva_geo(
        _criteria(location="Toronto, ON")
    )
    assert countries == ["CA"]
    assert states == ["ON"]
    assert zip_code == ""  # no US zip is ever synthesized for a CA job


@pytest.mark.parametrize("loc", [
    "Ajax, ON CA",
    "Ajax, Ontario, Canada",
    "Mississauga, ON, Canada",
])
def test_canadian_variants_resolve_to_ca_on(svc, loc):
    countries, states, _zip = svc._resolve_jobdiva_geo(_criteria(location=loc))
    assert countries == ["CA"]
    assert states == ["ON"]


def test_us_city_still_gets_state_and_zip(svc):
    countries, states, zip_code = svc._resolve_jobdiva_geo(
        _criteria(location="Tempe, AZ 85281")
    )
    assert countries == ["US"]
    assert states == ["AZ"]
    assert zip_code == "85281"


# ------------------------------------------------- _parse_location country

def test_parse_location_province_aliases(svc):
    parsed = svc._parse_location("Toronto, Ontario")
    assert parsed["state"] == "on"
    assert parsed["country"] == "CA"


def test_parse_location_country_in_state_slot(svc):
    parsed = svc._parse_location("Toronto, Canada")
    assert parsed["state"] == ""
    assert parsed["country"] == "CA"
    assert parsed["city"] == "toronto"


def test_parse_location_us_state_slot_not_country(svc):
    parsed = svc._parse_location("Los Angeles, CA")
    assert parsed["state"] == "ca"
    assert parsed["country"] == "US"


# ------------------------------------------------- external source scoping

def test_search_location_for_source_canadian_job(svc):
    loc = svc._search_location_for_source(_criteria(location="Toronto, ON"))
    assert loc == "Toronto, ON, Canada"


def test_search_location_for_source_remote_follows_country(svc):
    assert svc._search_location_for_source(
        _criteria(location="Toronto, ON", location_type="Remote")
    ) == "Canada"
    assert svc._search_location_for_source(
        _criteria(location="Plano, TX", location_type="Remote")
    ) == "United States"


def test_target_country_prefers_explicit_countries(svc):
    assert svc._target_country(_criteria(location="Plano, TX", countries=["CA"])) == "CA"
    assert svc._target_country(_criteria(location="Toronto, ON")) == "CA"
    assert svc._target_country(_criteria(location="Plano, TX")) == "US"
    assert svc._target_country(_criteria(location="")) == "US"


# ------------------------------------------------- outside-country gate

def test_outside_country_gate_us_job(svc):
    assert svc._is_likely_outside_country({"location": "Hyderabad, India"}, "US")
    assert svc._is_likely_outside_country({"location": "Toronto, Ontario, Canada"}, "US")
    # Positive evidence only — a bare US city or silence stays kept.
    assert not svc._is_likely_outside_country({"location": "Plano, TX"}, "US")
    assert not svc._is_likely_outside_country({}, "US")


def test_outside_country_gate_canadian_job(svc):
    # A Canadian job must KEEP Canadian candidates…
    assert not svc._is_likely_outside_country(
        {"location": "Toronto, Ontario, Canada"}, "CA"
    )
    assert not svc._is_likely_outside_country({"location": "Ajax, ON"}, "CA")
    # …and drop confirmed-foreign ones (incl. explicit US).
    assert svc._is_likely_outside_country({"location": "Hyderabad, India"}, "CA")
    assert svc._is_likely_outside_country({"location": "Dallas, United States"}, "CA")
    assert svc._is_likely_outside_country({"country": "US"}, "CA")


def test_country_field_unknown_is_not_evidence(svc):
    # JobDiva can return ids / free text in the country field — unparseable
    # values must not nuke the candidate.
    assert not svc._is_likely_outside_country({"country": "12345"}, "US")


# ------------------------------------------------- location precedence

def test_structured_locations_source_native_wins(svc):
    locs = svc._candidate_structured_locations({
        "city": "Ajax",
        "state": "ON",
        "location": "Ajax, ON",
        "enhanced_info": {"current_location": "Hyderabad, India"},
    })
    joined = " | ".join(locs).lower()
    assert "ajax" in joined
    # The LLM string must NOT ride alongside authoritative source data —
    # it would otherwise re-trigger the outside-country gate.
    assert "hyderabad" not in joined


def test_structured_locations_llm_fallback_when_source_blank(svc):
    locs = svc._candidate_structured_locations({
        "city": "",
        "state": "",
        "location": "",
        "enhanced_info": {"current_location": "Hyderabad, India"},
    })
    assert locs == ["Hyderabad, India"]


# ------------------------------------------------- hard-gate veto stamping

def test_location_hard_gate_stamps_veto_reason_state_mismatch(svc):
    cand = {"location": "Miami, FL"}
    veto = svc._location_hard_gate(cand, _criteria(location="AZ"))
    assert veto is not None
    assert cand.get("location_veto_reason") == "state_mismatch"


def test_location_hard_gate_stamps_confirmed_outside_radius(svc):
    cand = {"location": "Tucson, AZ"}  # offline-resolvable, ~100mi from Tempe
    veto = svc._location_hard_gate(cand, _criteria())
    assert veto is not None
    assert cand.get("location_veto_reason") == "outside_radius_confirmed"
    assert cand.get("location_out_of_radius") is True


def test_location_hard_gate_unknown_location_no_veto(svc):
    cand = {"location": ""}
    veto = svc._location_hard_gate(cand, _criteria())
    assert veto is None
    assert "location_veto_reason" not in cand


# ------------------------------------------- JobAgent location-veto exemption
# JobAgent results follow the criteria the recruiter authored inside JobDiva,
# so a confirmed location mismatch never zeroes their score (2026-08-25).
# The badge fields still stamp so the UI renders the distance.

def test_location_hard_gate_jobagent_out_of_radius_not_vetoed(svc):
    cand = {"location": "Tucson, AZ", "source": "JobDiva-JobAgent"}
    veto = svc._location_hard_gate(cand, _criteria())
    assert veto is None
    # Badge fields still stamped so the UI renders "~N mi away"…
    assert cand.get("location_out_of_radius") is True
    assert isinstance(cand.get("distance_miles"), float)
    # …but no machine-readable veto marker: nothing downstream may treat
    # this row as location-vetoed.
    assert "location_veto_reason" not in cand


def test_location_hard_gate_jobagent_state_mismatch_not_vetoed(svc):
    cand = {"location": "Miami, FL", "source": "JobDiva-JobAgent"}
    veto = svc._location_hard_gate(cand, _criteria(location="AZ"))
    assert veto is None
    assert "location_veto_reason" not in cand


def test_location_hard_gate_jobagent_flag_restores_veto(svc, monkeypatch):
    from core import sourcing_config
    monkeypatch.setattr(
        sourcing_config, "JOBAGENT_LOCATION_HARD_VETO", True, raising=False
    )
    cand = {"location": "Tucson, AZ", "source": "JobDiva-JobAgent"}
    veto = svc._location_hard_gate(cand, _criteria())
    assert veto is not None
    assert cand.get("location_veto_reason") == "outside_radius_confirmed"


@pytest.mark.parametrize("source", [
    "JobDiva-TalentSearch", "LinkedIn-Exa", "LinkedIn-Unipile", "Dice",
])
def test_location_hard_gate_other_sources_still_veto(svc, source):
    cand = {"location": "Tucson, AZ", "source": source}
    veto = svc._location_hard_gate(cand, _criteria())
    assert veto is not None, source
    assert cand.get("location_veto_reason") == "outside_radius_confirmed"
