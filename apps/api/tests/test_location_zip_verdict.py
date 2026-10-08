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


def test_city_state_centroid_resolves_colloquial_aliases():
    """"New York City" doesn't match GeoNames' "New York" entry and used to
    silently fail offline resolution, forcing every candidate checked against
    that anchor onto a live Nominatim call. Same class of gap for St./Ft./Mt.
    abbreviations and a few common colloquial short names."""
    aliased = {
        ("New York City", "NY"): ("New York", "NY"),
        ("NYC", "NY"): ("New York", "NY"),
        ("LA", "CA"): ("Los Angeles", "CA"),
        ("St. Louis", "MO"): ("Saint Louis", "MO"),
        ("St. Paul", "MN"): ("Saint Paul", "MN"),
        ("Ft. Worth", "TX"): ("Fort Worth", "TX"),
        ("Mt. Vernon", "NY"): ("Mount Vernon", "NY"),
        ("Mt. Pleasant", "SC"): ("Mount Pleasant", "SC"),
        ("SF", "CA"): ("San Francisco", "CA"),
        ("San Fran", "CA"): ("San Francisco", "CA"),
        ("Philly", "PA"): ("Philadelphia", "PA"),
        ("Vegas", "NV"): ("Las Vegas", "NV"),
        ("Nola", "LA"): ("New Orleans", "LA"),
        ("DC", "DC"): ("Washington", "DC"),
        ("Washington DC", "DC"): ("Washington", "DC"),
    }
    for (alias_city, alias_state), (canon_city, canon_state) in aliased.items():
        alias_point = zip_index.city_state_centroid(alias_city, alias_state)
        canon_point = zip_index.city_state_centroid(canon_city, canon_state)
        assert alias_point is not None, f"{alias_city}, {alias_state} did not resolve"
        assert alias_point == canon_point, (alias_city, alias_state)

    # Cities genuinely named with "City"/"St"/"Ft"/"Mt" in GeoNames must be
    # untouched — this must stay a targeted alias table, not a suffix strip.
    for city, state in [
        ("Jersey City", "NJ"), ("Kansas City", "MO"), ("Oklahoma City", "OK"),
        ("Carson City", "NV"), ("Atlantic City", "NJ"), ("Rapid City", "SD"),
        ("Salt Lake City", "UT"),
    ]:
        assert zip_index.city_state_centroid(city, state) is not None, (city, state)


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
    # Tucson is ~100 mi from Tempe — confirmed outside and rejected.
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tucson, AZ 85701"}, _criteria()
    )
    assert not ok and reason == "outside_radius_confirmed"
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
    assert not ok and reason == "candidate_location_missing" and dist == 9999.0


@pytest.mark.parametrize("location", [
    "Tri-State Area",
    "Midwest Region",
])
def test_verdict_broad_region_is_unverified_without_geocoding(svc, monkeypatch, location):
    import services.unified_candidate_search as ucs

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("broad region labels must not be treated as point locations")

    monkeypatch.setattr(ucs, "within_radius", unexpected_geocode)
    ok, reason, distance = svc._location_match_verdict(
        {"location": location}, _criteria()
    )
    assert not ok
    assert reason == "broad_region_unverified"
    assert distance == 9999.0


@pytest.mark.parametrize("location", [
    "San Francisco Bay Area",
    "Greater Chicago Area",
    "Los Angeles Metropolitan Area",
    "Seattle Metro Area",
    "Chicagoland",
    "Silicon Valley",
])
def test_verdict_named_region_outside_search_state_is_mismatch(svc, monkeypatch, location):
    """A LinkedIn metro whose states miss the search location is a confirmed
    mismatch, not an unverified keep. Tempe, AZ does not cover IL/CA/WA."""
    import services.unified_candidate_search as ucs

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("broad region labels must not be treated as point locations")

    monkeypatch.setattr(ucs, "within_radius", unexpected_geocode)
    ok, reason, _distance = svc._location_match_verdict(
        {"location": location}, _criteria()
    )
    assert not ok and reason == "state_mismatch", (location, reason)


def test_broad_region_with_state_suffix_still_uses_state_but_not_radius(svc, monkeypatch):
    import services.unified_candidate_search as ucs

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("a broad region label must not be geocoded as a point")

    monkeypatch.setattr(ucs, "within_radius", unexpected_geocode)
    ok, reason, distance = svc._location_match_verdict(
        {"location": "San Francisco Bay Area, CA"},
        _criteria(location="Mountain View, CA 94043"),
    )
    assert not ok and reason == "broad_region_unverified"
    assert distance == 9999.0


def test_precise_city_signal_overrides_broad_region_label(svc):
    ok, reason, distance = svc._location_match_verdict(
        {"location": "San Francisco Bay Area", "city": "Tucson", "state": "AZ"},
        _criteria(),
    )
    assert not ok and reason == "outside_radius_confirmed"
    assert distance is not None and distance > 25


def test_verdict_state_only_matches_via_direct_zip(svc):
    ok, reason, _ = svc._location_match_verdict(
        {"zipcode": "85004"}, _criteria(location="AZ")
    )
    assert ok and reason == "state_match"


def test_verdict_open_to_relocation_does_not_bypass_configured_radius(svc):
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tucson, AZ 85701", "open_to_relocation": True},
        _criteria(include_relocation_candidates=False),
    )
    assert not ok and reason == "outside_radius_confirmed"
    assert dist is not None and dist > 25


def test_configured_mountain_view_new_york_phoenix_radii(svc):
    criteria = _criteria(
        location="Mountain View, CA 94043",
        within_miles=25,
        additional_locations=[
            {"value": "New York, NY", "within_miles": 50},
            {"value": "Phoenix, AZ", "within_miles": 50},
        ],
    )
    for location in ("San Jose, CA", "Pleasanton, CA"):
        ok, reason, distance = svc._location_match_verdict({"location": location}, criteria)
        assert ok, (location, reason, distance)
        assert distance is not None and distance <= 25

    for location in ("San Francisco, CA", "Buffalo, NY"):
        ok, reason, distance = svc._location_match_verdict({"location": location}, criteria)
        assert not ok, (location, reason, distance)
        assert distance is not None and distance > 25

    for location in ("New York, NY", "Phoenix, AZ"):
        ok, reason, distance = svc._location_match_verdict({"location": location}, criteria)
        assert ok, (location, reason, distance)


def test_new_york_city_alias_closes_multi_location_soft_keep_leak(svc, monkeypatch):
    """Regression for the QA-reported leak: with "New York City, NY" configured
    as one of 5 sourcing anchors (Hartford 25mi, NYC 41mi, LA 50mi, Chicago
    70mi, Phoenix 70mi), a candidate confirmed-outside every radius was
    surviving as "geocode_unavailable" soft-keep. Root cause: the NYC anchor
    didn't resolve offline, so its check fell to Nominatim; when that network
    call failed/rate-limited, the resulting soft-keep out-ranked the other 4
    anchors' correct hard-drops (soft-keep always beats hard-drop in
    _location_match_verdict's precedence, by design). Fixing the alias makes
    the NYC anchor resolve offline like the other 4, so there is no longer any
    soft-keep left to win the race."""
    import services.unified_candidate_search as ucs

    def boom(*a, **kw):
        raise AssertionError("all 5 anchors must resolve offline — no Nominatim needed")

    monkeypatch.setattr(ucs, "within_radius", boom)

    criteria = _criteria(
        location="Hartford, CT",
        within_miles=25,
        additional_locations=[
            {"value": "New York City, NY", "within_miles": 41},
            {"value": "Los Angeles, CA", "within_miles": 50},
            {"value": "Chicago, IL", "within_miles": 70},
            {"value": "Phoenix, AZ", "within_miles": 70},
        ],
    )

    # Outside all 5 configured radii — must hard-drop, not soft-keep.
    # Deliberately only towns with a comfortable (8+ mi) margin past every
    # anchor's cap — Norwalk/Fairfield/Trumbull/Stratford sit within a few
    # miles of the 41 mi NYC radius (Norwalk is ~0.3 mi over) and were
    # dropped from this list per review feedback: a GeoNames centroid refresh
    # could flip their verdict for reasons unrelated to the alias fix this
    # test exists to cover, making the test flaky.
    for location in (
        "New Haven, CT", "Branford, CT", "Mystic, CT", "North Stonington, CT",
        "Putnam, CT", "Woodstock, CT", "Danielson, CT", "Moosup, CT",
        "Norwich, CT", "Essex, CT",
    ):
        ok, reason, distance = svc._location_match_verdict({"location": location}, criteria)
        assert not ok and reason == "outside_radius_confirmed", (location, reason, distance)

    # Legitimately inside exactly one anchor's radius — must still pass.
    for location, via in (
        ("Stamford, CT", "NYC"), ("Greenwich, CT", "NYC"), ("New Canaan, CT", "NYC"),
        ("Torrington, CT", "Hartford"), ("Waterbury, CT", "Hartford"),
    ):
        ok, reason, distance = svc._location_match_verdict({"location": location}, criteria)
        assert ok, (location, via, reason, distance)


def test_step5_location_gate_removes_candidates_outside_every_radius(svc):
    from unittest.mock import MagicMock

    svc._log_stage = MagicMock()
    criteria = _criteria(
        location="Mountain View, CA 94043",
        within_miles=25,
        additional_locations=[
            {"value": "New York, NY", "within_miles": 50},
            {"value": "Phoenix, AZ", "within_miles": 50},
        ],
    )
    candidates = [
        {"location": "San Jose, CA", "candidate_id": "san-jose"},
        {"location": "Pleasanton, CA", "candidate_id": "pleasanton"},
        {"location": "New York, NY", "candidate_id": "new-york"},
        {"location": "Phoenix, AZ", "candidate_id": "phoenix"},
        {"location": "Buffalo, NY", "candidate_id": "buffalo"},
    ]

    kept = svc._filter_by_state(candidates, criteria)

    assert {candidate["candidate_id"] for candidate in kept} == {
        "san-jose", "pleasanton", "new-york", "phoenix"
    }


def test_hard_gate_vetoes_offline_confirmed_outside(svc):
    cand = {"location": "Tucson, AZ 85701"}
    veto = svc._location_hard_gate(cand, _criteria())
    assert veto and "outside" in veto
    assert isinstance(cand.get("distance_miles"), float)


def test_hard_gate_soft_keeps_broad_region_as_unverified(svc):
    """Same-state region stays unverified. A different state's region drops."""
    candidate = {"location": "Phoenix Metropolitan Area"}
    assert svc._location_hard_gate(candidate, _criteria()) is None
    assert candidate.get("location_match_reason") == "broad_region_unverified"
    assert candidate.get("location_out_of_radius") is not True
    assert candidate.get("distance_miles") is None

    chicago = {"location": "Greater Chicago Area"}
    assert svc._location_hard_gate(chicago, _criteria())
    assert chicago.get("location_veto_reason") == "state_mismatch"


def test_hard_gate_soft_keeps_county_and_metroplex_as_unverified(svc):
    """"Santa Clara County, CA" and "Dallas-Fort Worth Metroplex" aren't cities.
    Before the broad-region regex covered "County" and "Metroplex", these
    fell through to a live Nominatim call and their soft-keep status was
    incidental (dependent on that call failing), not deterministic like
    "Bay Area"/"Metro Area" strings."""
    county = {"location": "Santa Clara County, CA"}
    assert svc._location_hard_gate(
        county, _criteria(location="Los Angeles, CA", within_miles=50)
    ) is None
    assert county.get("location_match_reason") == "broad_region_unverified"
    assert county.get("distance_miles") is None

    # DFW is Texas. Against a Los Angeles search that is a state mismatch.
    # Against a Dallas search the label is kept verbatim and unverified —
    # it must not be rewritten to "Fort Worth Metroplex, US".
    dfw_far = {"location": "Dallas-Fort Worth Metroplex"}
    assert svc._location_hard_gate(
        dfw_far, _criteria(location="Los Angeles, CA", within_miles=50)
    )
    assert dfw_far.get("location_veto_reason") == "state_mismatch"
    dfw_near = {"location": "Dallas-Fort Worth Metroplex"}
    assert svc._location_hard_gate(
        dfw_near, _criteria(location="Richardson, TX", within_miles=25)
    ) is None
    assert dfw_near.get("location_match_reason") == "broad_region_unverified"
    assert dfw_near.get("location") == "Dallas-Fort Worth Metroplex"


def test_broad_region_bare_literals_do_not_over_match_prefixed_strings():
    """"chicagoland" and "silicon valley" are bare literals (no leading `.+`
    wildcard) in _BROAD_REGION_RE, matched via .fullmatch(). A prefixed
    variant like "North Chicagoland" must NOT match — there's nothing in the
    pattern to consume the "North " prefix. If this ever starts failing, the
    regex has grown a `.+` that makes these over-match (review flagged this
    exact risk for PR #769)."""
    from services.location import is_broad_region_location

    assert is_broad_region_location("Chicagoland") is True
    assert is_broad_region_location("Chicagoland, IL") is True
    assert is_broad_region_location("Silicon Valley") is True
    assert is_broad_region_location("Silicon Valley, CA") is True
    assert is_broad_region_location("North Chicagoland") is False
    assert is_broad_region_location("South Chicagoland, IL") is False
    assert is_broad_region_location("North Silicon Valley") is False


def test_hard_gate_no_veto_for_remote_job(svc):
    assert svc._location_hard_gate(
        {"location": "Miami, FL"}, _criteria(location_type="Remote")
    ) is None


def test_hard_gate_allows_unknown_location_for_remote_job(svc):
    candidate = {}
    assert svc._location_hard_gate(
        candidate, _criteria(location_type="Remote")
    ) is None


def test_hard_gate_soft_keeps_transient_geocoder_failure(svc, monkeypatch):
    import services.unified_candidate_search as ucs

    monkeypatch.setattr(
        ucs, "within_radius",
        lambda *args, **kwargs: (False, "candidate_ungeocodable", None),
    )
    import services.location as loc
    monkeypatch.setattr(loc, "is_plausible_city_token", lambda *args, **kwargs: True)
    candidate = {"location": "Nopeville, CA"}
    assert svc._location_hard_gate(candidate, _criteria()) is None
    assert candidate.get("location_match_reason") == "geocode_unavailable"
    assert "location_veto_reason" not in candidate


# ---------------------------------------------- review-confirmed regressions

def test_verdict_resume_location_is_judged_when_present(svc, monkeypatch):
    """Policy 2026-09-11: the résumé is final for residence. When the LLM
    extracted an explicit `enhanced_info.current_location`, THAT is the
    location the verdict judges — the source-native `candidate.location`
    ("Tucson, AZ" here) is not consulted alongside it. (Superseded the
    2026-07-30 source-native-wins policy by product request: JobDiva agent
    records saying "US" for résumés headed "India" were being launched.)"""
    import services.unified_candidate_search as ucs

    geocoded = []

    def unexpected_geocode(candidate_loc, target, miles):
        geocoded.append(candidate_loc)
        raise AssertionError("a broad metro label must not be geocoded as an exact point")

    monkeypatch.setattr(ucs, "within_radius", unexpected_geocode)
    ok, reason, dist = svc._location_match_verdict(
        {
            "enhanced_info": {"current_location": "Phoenix Metropolitan Area"},
            "location": "Tucson, AZ",
        },
        _criteria(),
    )
    # The broad résumé region remains visible but cannot replace an exact
    # point or condemn the candidate; the structured Tucson location is
    # intentionally ignored under the résumé-authoritative policy.
    assert not ok and reason == "broad_region_unverified"
    assert dist == 9999.0
    assert geocoded == []


def test_verdict_source_native_location_beats_llm_extraction_when_flag_off(svc, monkeypatch):
    """RESUME_LOCATION_AUTHORITATIVE=False restores policy 2026-07-30 (Job
    26-22448): the source-native location is authoritative and the
    LLM-extracted string is NOT consulted while `candidate.location` is
    present — it can neither rescue nor condemn the candidate."""
    import services.unified_candidate_search as ucs
    from core import sourcing_config

    monkeypatch.setattr(sourcing_config, "RESUME_LOCATION_AUTHORITATIVE", False)
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
    # Tucson resolves offline ~100mi from Tempe → confirmed-outside signal
    # (soft-keep verdict; _location_hard_gate turns the real distance into a
    # veto). The LLM string is ignored entirely — no Nominatim call, so the
    # unresolvable "Phoenix Metropolitan Area" cannot rescue the row.
    assert not ok and reason == "outside_radius_confirmed"
    assert dist is not None and dist > 25
    assert geocoded == []


def test_verdict_broad_resume_location_is_unverified_when_source_blank(svc, monkeypatch):
    """A broad résumé location is displayable, but not precise enough to
    confirm or reject a configured radius."""
    import services.unified_candidate_search as ucs

    geocoded = []

    def unexpected_geocode(candidate_loc, target, miles):
        geocoded.append(candidate_loc)
        raise AssertionError("broad metro label must not be geocoded")

    monkeypatch.setattr(ucs, "within_radius", unexpected_geocode)
    ok, reason, dist = svc._location_match_verdict(
        {
            "enhanced_info": {"current_location": "Phoenix Metropolitan Area"},
            "location": "",
        },
        _criteria(),
    )
    assert not ok and reason == "broad_region_unverified"
    assert dist == 9999.0
    assert geocoded == []


def test_verdict_all_signals_offline_skips_nominatim(svc, monkeypatch):
    import services.unified_candidate_search as ucs

    def boom(*a, **kw):
        raise AssertionError("Nominatim must not be called when all signals resolve offline")

    monkeypatch.setattr(ucs, "within_radius", boom)
    ok, reason, dist = svc._location_match_verdict(
        {"location": "Tucson, AZ 85701"}, _criteria()
    )
    assert not ok and reason == "outside_radius_confirmed" and dist > 25


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


def _qa_criteria():
    return _criteria(
        location="Richardson, TX",
        within_miles=25,
        additional_locations=[
            {"value": "Pennington, NJ", "within_miles": 50},
            {"value": "New York, NY 10006", "within_miles": 60},
            {"value": "Phoenix, AZ", "within_miles": 60},
        ],
    )


def test_qa_radius_uses_representative_city_point_not_zip_average(svc):
    """City-only names are measured from a real ZIP in each city.

    Richardson's ZIP average sits south of the city and was keeping
    DeSoto and Cedar Hill inside 25 miles. Melissa stays inside.
    """
    criteria = _qa_criteria()
    kept = ["Melissa, TX", "Plano, TX", "Euless, TX", "Prosper, TX", "Frisco, TX"]
    dropped = ["DeSoto, TX", "Desoto, TX", "Cedar Hill, TX", "Arlington, TX", "Fort Worth, TX", "Austin, TX"]
    for location in kept:
        ok, reason, _dist = svc._location_match_verdict({"location": location}, criteria)
        assert ok, (location, reason, _dist)
    for location in dropped:
        veto = svc._location_hard_gate({"location": location}, criteria)
        assert veto, location


def test_greater_philadelphia_inside_pennington_radius_stays_unverified(svc, monkeypatch):
    """Philadelphia is in Pennsylvania and inside 50 miles of Pennington, NJ.

    The metro label must not hard-drop on the state line. Chicago is outside
    that circle, so it still drops.
    """
    import services.unified_candidate_search as ucs

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("broad region labels must not be treated as point locations")

    monkeypatch.setattr(ucs, "within_radius", unexpected_geocode)
    pennington = _criteria(location="Pennington, NJ", within_miles=50)
    ok, reason, _distance = svc._location_match_verdict(
        {"location": "Greater Philadelphia Area"}, pennington
    )
    assert not ok and reason == "broad_region_unverified", (ok, reason)
    assert svc._location_hard_gate(
        {"location": "Greater Philadelphia Area"}, pennington
    ) is None

    chicago = svc._location_hard_gate(
        {"location": "Greater Chicago Area"}, pennington
    )
    assert chicago


def test_qa_linkedin_strings_are_judged_not_left_blank(svc):
    criteria = _qa_criteria()
    edison = svc._location_match_verdict(
        {"location": "Edison, New Jersey, United States"}, criteria
    )
    assert edison[0] is True, edison

    poughkeepsie = svc._location_hard_gate(
        {"location": "Poughkeepsie, New York, United States"}, criteria
    )
    assert poughkeepsie

    # Within 50 miles of Pennington, so a different state is still a keep.
    # Cross-state metros (Jersey City, Allentown) follow the radius, not a
    # blanket state mismatch that would also drop Jersey City.
    allentown = svc._location_match_verdict(
        {"location": "Allentown, Pennsylvania, United States"}, criteria
    )
    assert allentown[0] is True, allentown

    for label in ("New York, United States", "New Jersey, United States", "New Jersey, USA", "United States"):
        ok, reason, _dist = svc._location_match_verdict({"location": label}, criteria)
        assert not ok and reason == "broad_region_unverified", (label, reason)

    chicago = svc._location_hard_gate({"location": "Greater Chicago Area"}, criteria)
    detroit = svc._location_hard_gate({"location": "Detroit Metropolitan Area"}, criteria)
    assert chicago and detroit

    hyderabad = svc._is_likely_outside_country(
        {"location": "Hyderabad, Telangana, India"}, "US"
    )
    assert hyderabad is True

    # A city name with no state stays on the card and is not pinned to one
    # of several possible states. A city that exists in only one state is
    # measured.
    edison_bare = svc._location_match_verdict({"location": "Edison"}, criteria)
    assert edison_bare[1] == "candidate_state_unknown", edison_bare
    assert svc._location_hard_gate({"location": "Edison"}, criteria) is None

    tempe = svc._location_match_verdict({"location": "Tempe"}, criteria)
    assert tempe[0] is True, tempe
    secaucus = svc._location_match_verdict({"location": "Secaucus"}, criteria)
    assert secaucus[0] is True, secaucus
    irving = svc._location_match_verdict({"location": "Irving"}, criteria)
    assert irving[1] == "candidate_state_unknown", irving


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
