"""Unit tests for zip/pincode-based location matching (2026-07).

Covers: the offline zip index, the _resolve_jobdiva_geo zip/state fix
("Tempe, AZ 85281" used to silently drop the state), the offline-centroid
upgrade of _location_match_verdict, the remote-job skip, the direct
candidate-zipcode signal, the boolean zip-dialect rewrite, and the Exa
query zip stripping.

Same harness pattern as test_score_candidate_rubric.py: the service's
__init__ touches external clients, so we build a bare instance via
object.__new__ and exercise the pure methods directly.
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

from services import zip_index  # noqa: E402
from services.exa_service import compose_people_query, _strip_zip_for_query  # noqa: E402
from services.jobdiva_boolean_translator import (  # noqa: E402
    count_location_clauses,
    rewrite_location_clauses_to_zip_dialect,
)
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


# ---------------------------------------------------------------- zip index

def test_zip_index_lookup_and_distance():
    entry = zip_index.lookup_zip("85281")
    assert entry and entry["city"] == "Tempe" and entry["state"] == "AZ"
    # Tempe → downtown Phoenix is well under 25 mi.
    d = zip_index.zip_distance_miles("85281", "85004")
    assert d is not None and 3 < d < 20
    assert zip_index.zip_distance_miles("85281", "00000") is None


def test_zip_index_extract_validates_against_real_zips():
    assert zip_index.extract_zip("Tempe, AZ 85281 within 25 mi") == "85281"
    # 90000 is not an assigned zip — must not be treated as one.
    assert zip_index.extract_zip("salary 90000 in Tempe") is None


def test_city_state_centroid_and_default_zip():
    assert zip_index.city_state_centroid("Tempe", "AZ") is not None
    assert zip_index.city_state_centroid("tempe", "Arizona") is not None
    assert zip_index.city_state_centroid("Notacity", "AZ") is None
    rep = zip_index.city_state_default_zip("Plano", "TX")
    assert rep and zip_index.lookup_zip(rep)["city"] == "Plano"


# ------------------------------------------------------- _resolve_jobdiva_geo

def test_resolve_geo_zip_no_longer_swallows_state(svc):
    """Regression: 'Tempe, AZ 85281' produced states=[] because the token
    'AZ 85281' failed the len==2 state-code check."""
    countries, states, zip_code = svc._resolve_jobdiva_geo(_criteria())
    assert countries == ["US"]
    assert states == ["AZ"]
    assert zip_code == "85281"


def test_resolve_geo_full_state_name(svc):
    _, states, _ = svc._resolve_jobdiva_geo(_criteria(location="Phoenix, Arizona"))
    assert states == ["AZ"]


def test_resolve_geo_bare_zip_backfills_state(svc):
    countries, states, zip_code = svc._resolve_jobdiva_geo(_criteria(location="85281"))
    assert zip_code == "85281"
    assert states == ["AZ"]
    assert countries == ["US"]


def test_resolve_geo_city_without_zip_gets_representative_zip(svc):
    _, states, zip_code = svc._resolve_jobdiva_geo(_criteria(location="Plano, TX"))
    assert states == ["TX"]
    assert zip_code and zip_index.lookup_zip(zip_code)["city"] == "Plano"


def test_resolve_geo_explicit_states_short_circuit(svc):
    countries, states, zip_code = svc._resolve_jobdiva_geo(
        _criteria(states=["TX"], countries=["US"])
    )
    assert states == ["TX"] and countries == ["US"]
    assert zip_code == "85281"  # zip still extracted from location


def test_resolve_geo_empty_location(svc):
    assert svc._resolve_jobdiva_geo(_criteria(location="")) == (["US"], [], "")


# --------------------------------------------------- _location_match_verdict

def test_verdict_exact_zip_match(svc):
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tempe, AZ 85281"}, _criteria()
    )
    assert ok and reason == "zip_match" and dist == 0.0


def test_verdict_nearby_zip_within_radius_offline(svc):
    # 85004 (downtown Phoenix) is ~9 mi from 85281 — inside the 25 mi radius.
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Phoenix, AZ 85004"}, _criteria()
    )
    assert ok and reason == "within_radius"
    assert dist is not None and 0 < dist < 25


def test_verdict_far_zip_confirmed_outside_offline(svc):
    # Tucson is ~100 mi from Tempe — confirmed outside, soft-kept, real distance.
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tucson, AZ 85701"}, _criteria()
    )
    assert ok and reason == "outside_radius_soft_keep"
    assert dist is not None and dist > 25


def test_verdict_city_state_without_zip_resolves_offline(svc):
    # No zips anywhere — city centroids alone must produce a distance.
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Chandler, AZ"}, _criteria(location="Tempe, AZ")
    )
    assert ok and reason == "within_radius"
    assert dist is not None and dist < 25


def test_verdict_uses_direct_jobdiva_zipcode_field(svc):
    # Blank location strings but a JobDiva zipcode → distance still resolved.
    ok, reason, dist = svc._location_match_verdict(
        {"location": "", "city": "", "state": "", "zipcode": "85004"}, _criteria()
    )
    assert ok and reason == "within_radius"
    assert dist is not None and dist < 25


def test_verdict_direct_zipcode_exact_match(svc):
    ok, reason, dist = svc._location_match_verdict(
        {"zipcode": "85281"}, _criteria()
    )
    assert ok and reason == "zip_match" and dist == 0.0


def test_verdict_bare_zip_criteria_not_treated_as_empty(svc):
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tempe, AZ"}, _criteria(location="85281")
    )
    assert ok and reason in ("city_state_match", "within_radius")


def test_verdict_remote_job_skips_radius(svc):
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Miami, FL"}, _criteria(location_type="Remote")
    )
    assert ok and reason == "remote_job_no_location_constraint" and dist is None


def test_verdict_missing_location_soft_keep_sentinel(svc):
    ok, reason, dist = svc._location_match_verdict({"location": ""}, _criteria())
    assert ok and reason == "candidate_location_missing_keep" and dist == 9999.0


def test_verdict_state_only_matches_via_direct_zip(svc):
    ok, reason, _ = svc._location_match_verdict(
        {"zipcode": "85004"}, _criteria(location="AZ")
    )
    assert ok and reason == "state_match"


def test_verdict_relocation_optout_blocks_confirmed_outside(svc):
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tucson, AZ 85701", "open_to_relocation": True},
        _criteria(include_relocation_candidates=False),
    )
    assert not ok and reason == "relocation_excluded_by_filter"
    assert dist is not None and dist > 25


def test_hard_gate_vetoes_offline_confirmed_outside(svc):
    cand = {"location": "Tucson, AZ 85701"}
    veto = svc._location_hard_gate(cand, _criteria())
    assert veto and "outside" in veto
    assert isinstance(cand.get("distance_miles"), float)


def test_hard_gate_no_veto_for_remote_job(svc):
    assert svc._location_hard_gate(
        {"location": "Miami, FL"}, _criteria(location_type="Remote")
    ) is None


# ---------------------------------------------- review-confirmed regressions

def test_verdict_unresolved_higher_priority_signal_still_geocodes(svc, monkeypatch):
    """Review bug 1: a stale-but-offline-resolvable signal ("Tucson, AZ")
    must not confirm the candidate outside while an offline-unresolvable
    signal ("Phoenix Metropolitan Area") would geocode in-radius."""
    import services.unified_candidate_search as ucs

    geocoded = []

    def fake_within_radius(candidate_loc, target, miles):
        geocoded.append(candidate_loc)
        return True, "ok", 16.3

    monkeypatch.setattr(ucs, "within_radius", fake_within_radius)
    ok, reason, dist = svc._location_match_verdict(
        {
            "enhanced_info": {"current_location": "Phoenix Metropolitan Area"},
            "location": "Tucson, AZ",
        },
        _criteria(),
    )
    assert ok and reason == "within_radius"
    assert dist == 16.3
    # Only the offline-unresolvable string goes to Nominatim.
    assert geocoded == ["Phoenix Metropolitan Area"]


def test_verdict_all_signals_offline_skips_nominatim(svc, monkeypatch):
    import services.unified_candidate_search as ucs

    def boom(*a, **kw):
        raise AssertionError("Nominatim must not be called when all signals resolve offline")

    monkeypatch.setattr(ucs, "within_radius", boom)
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tucson, AZ 85701"}, _criteria()
    )
    assert ok and reason == "outside_radius_soft_keep" and dist > 25


def test_parse_location_street_number_not_mistaken_for_zip(svc):
    """Review bug 2: '10001 W Main St, Mesa, AZ' — 10001 is a Manhattan zip
    but here it's a street number; the state cross-check must drop it."""
    parsed = svc._parse_location("10001 W Main St, Mesa, AZ")
    assert parsed["zip"] == ""


def test_parse_location_takes_trailing_zip_in_address(svc):
    parsed = svc._parse_location("10001 W Main St, Mesa, AZ 85201")
    assert parsed["zip"] == "85201"


def test_parse_location_zip_kept_when_state_agrees(svc):
    assert svc._parse_location("Tempe, AZ 85281")["zip"] == "85281"


def test_resolve_geo_address_street_number_not_sent_as_zip(svc):
    _, states, zip_code = svc._resolve_jobdiva_geo(
        _criteria(location="10001 W Main St, Mesa, AZ")
    )
    assert states == ["AZ"]
    assert zip_code != "10001"


def test_dialect_rewrite_rejects_street_number_zip_collision():
    src = '"10001 W Main St, Mesa, AZ" within 25 mi'
    out = rewrite_location_clauses_to_zip_dialect(src)
    assert "Within 25 miles of 10001" not in out


def test_hard_gate_stamps_badge_fields(svc):
    cand = {"location": "Tucson, AZ 85701"}
    svc._location_hard_gate(cand, _criteria())
    assert cand.get("location_out_of_radius") is True
    assert isinstance(cand.get("distance_miles"), float)
    assert cand["distance_miles"] > 25


# ------------------------------------------------------ boolean zip dialect

def test_dialect_rewrite_zip_in_phrase():
    out = rewrite_location_clauses_to_zip_dialect(
        '("Python" OR "Java") AND "Tempe, AZ 85281" within 25 mi'
    )
    assert out == '("Python" OR "Java") AND Within 25 miles of 85281'


def test_dialect_rewrite_city_only_uses_representative_zip():
    out = rewrite_location_clauses_to_zip_dialect('"Plano, TX" within 30 mi AND "Snowflake"')
    assert '"Plano, TX"' not in out
    assert "Within 30 miles of 75" in out  # some Plano-area zip


def test_dialect_rewrite_leaves_non_geo_phrases_alone():
    src = '"Mainframe" AND "REMOTE" within 25 mi'
    assert rewrite_location_clauses_to_zip_dialect(src) == src


def test_count_location_clauses():
    """Multi-chip guard: ≥2 clauses means the structured single-zip anchor
    must not be attached to the TalentSearch payload."""
    assert count_location_clauses('"Python" AND "Tempe, AZ 85281" within 25 mi') == 1
    assert count_location_clauses(
        '("Tempe, AZ 85281" within 25 mi OR "Dallas, TX 75201" within 25 mi)'
    ) == 2
    assert count_location_clauses('"Python" AND "Java"') == 0
    assert count_location_clauses("") == 0


def test_parse_location_foreign_postal_not_us_anchor(svc):
    # 75001 is Addison, TX — but this string is Paris, France.
    parsed = svc._parse_location("Paris, 75001, France")
    assert parsed["zip"] == ""


# ----------------------------------------------------------------- Exa query

def test_strip_zip_for_query():
    assert _strip_zip_for_query("Tempe, AZ 85281, United States") == "Tempe, AZ, United States"
    assert _strip_zip_for_query("Tempe, AZ") == "Tempe, AZ"
    assert _strip_zip_for_query("") == ""


def test_people_query_drops_zip_from_location():
    # The NL people-query (used by both Exa search + deep-research) must not
    # carry a zip — LinkedIn location lines never show them.
    q = compose_people_query("Data Engineer", location="Tempe, AZ 85281, United States")
    assert "85281" not in q
    assert "based in Tempe, AZ, United States" in q
