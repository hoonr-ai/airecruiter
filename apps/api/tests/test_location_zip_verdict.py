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
    _UNKNOWN_DISTANCE_SENTINEL,
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
    import services.unified_candidate_search.location as ucs_location

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("broad region labels must not be treated as point locations")

    monkeypatch.setattr(ucs_location, "within_radius", unexpected_geocode)
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
    import services.unified_candidate_search.location as ucs_location

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("broad region labels must not be treated as point locations")

    monkeypatch.setattr(ucs_location, "within_radius", unexpected_geocode)
    ok, reason, _distance = svc._location_match_verdict(
        {"location": location}, _criteria()
    )
    assert not ok and reason == "state_mismatch", (location, reason)


def test_broad_region_with_state_suffix_still_uses_state_but_not_radius(svc, monkeypatch):
    import services.unified_candidate_search.location as ucs_location

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("a broad region label must not be geocoded as a point")

    monkeypatch.setattr(ucs_location, "within_radius", unexpected_geocode)
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
    import services.unified_candidate_search.location as ucs_location

    def boom(*a, **kw):
        raise AssertionError("all 5 anchors must resolve offline — no Nominatim needed")

    monkeypatch.setattr(ucs_location, "within_radius", boom)

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


def test_hard_gate_drops_same_state_far_metro_confirmed_outside(svc):
    """QA regression: a multi-ZIP search for 10001 New York / 90001 Los
    Angeles / 77002 Houston kept "Dallas-Fort Worth Metroplex" and "Austin,
    Texas Metropolitan Area" candidates — both Texas, same state as the
    Houston ZIP, but ~146-225mi from it, a different metro entirely. Before
    this fix, a same-state broad region never checked its core city's
    distance at all and fell straight to broad_region_unverified (soft-
    keep) regardless of how far away it actually was — only a state
    MISMATCH triggered the core-distance check. Now it's checked either
    way, so a confirmed-far same-state metro hard-drops too."""
    dfw_vs_houston = {"location": "Dallas-Fort Worth Metroplex"}
    veto = svc._location_hard_gate(dfw_vs_houston, _criteria(location="Houston, TX", within_miles=50))
    assert veto and "outside" in veto
    assert dfw_vs_houston.get("location_veto_reason") == "outside_radius_confirmed"
    assert dfw_vs_houston.get("distance_miles") > 50

    austin_vs_houston = {"location": "Austin, Texas Metropolitan Area"}
    veto = svc._location_hard_gate(austin_vs_houston, _criteria(location="Houston, TX", within_miles=50))
    assert veto and "outside" in veto
    assert austin_vs_houston.get("location_veto_reason") == "outside_radius_confirmed"
    assert austin_vs_houston.get("distance_miles") > 50


def test_hard_gate_soft_keeps_a_nearby_suburb_search_against_a_metro_label(svc):
    """PR #859 review: a broad label's one core-city point stands in for a
    whole metro, which genuinely spans well beyond that single point —
    Fort Worth is ~32mi from the Dallas core both carry the same
    "Dallas-Fort Worth Metroplex" label. Hard-dropping purely on
    core-to-search distance would wrongly drop a real in-metro suburb
    search; _BROAD_REGION_CORE_BUFFER_MILES exists so this stays a
    soft-keep instead, the same as it was before PR #859's core-distance
    check started running for the same-state case at all."""
    dfw_vs_fort_worth = {"location": "Dallas-Fort Worth Metroplex"}
    assert svc._location_hard_gate(
        dfw_vs_fort_worth, _criteria(location="Fort Worth, TX", within_miles=25)
    ) is None
    assert dfw_vs_fort_worth.get("location_match_reason") == "broad_region_unverified"


def test_hard_gate_drops_newly_recognized_regions_in_the_wrong_state(svc):
    """QA regression: "Boise Metropolitan Area" (ID) against a Dallas/
    Phoenix/San Diego/Philadelphia search, and "Greater Tampa Bay Area"
    (FL) against a New York/Los Angeles/Houston search, were both soft-kept
    instead of hard-dropped — neither label was in _REGION_STATE_LABELS/
    _REGION_CORE_CITY, so the gate had no state or distance signal for them
    at all and fell through to broad_region_unverified unconditionally."""
    boise = {"location": "Boise Metropolitan Area"}
    veto = svc._location_hard_gate(boise, _criteria(location="Dallas, TX", within_miles=50))
    assert veto
    assert boise.get("location_veto_reason") == "state_mismatch"

    tampa = {"location": "Greater Tampa Bay Area"}
    veto = svc._location_hard_gate(tampa, _criteria(location="Houston, TX", within_miles=50))
    assert veto
    assert tampa.get("location_veto_reason") == "state_mismatch"


def test_region_state_labels_and_core_city_tables_stay_in_sync():
    """PR #859 review: _REGION_STATE_LABELS and _REGION_CORE_CITY
    (services/location.py) are two hand-maintained dicts that must agree —
    a label with a known state but no known core city can never get the
    distance check that tells a confirmed-far same-state metro apart from
    a nearby in-metro suburb (see _broad_core_radius_distance). Catches the
    kind of drift that's easy to introduce when adding a new label to only
    one of the two tables."""
    from services.location import _REGION_CORE_CITY, _REGION_STATE_LABELS

    missing_core = set(_REGION_STATE_LABELS) - set(_REGION_CORE_CITY)
    assert not missing_core, f"labels with a state but no core city: {missing_core}"
    missing_state = set(_REGION_CORE_CITY) - set(_REGION_STATE_LABELS)
    assert not missing_state, f"labels with a core city but no state: {missing_state}"


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
    import services.unified_candidate_search.location as ucs_location

    monkeypatch.setattr(
        ucs_location, "within_radius",
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
    import services.unified_candidate_search.location as ucs_location

    geocoded = []

    def unexpected_geocode(candidate_loc, target, miles):
        geocoded.append(candidate_loc)
        raise AssertionError("a broad metro label must not be geocoded as an exact point")

    monkeypatch.setattr(ucs_location, "within_radius", unexpected_geocode)
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
    import services.unified_candidate_search.location as ucs_location
    from core import sourcing_config

    monkeypatch.setattr(sourcing_config, "RESUME_LOCATION_AUTHORITATIVE", False)
    geocoded = []

    def fake_within_radius(candidate_loc, target, miles):
        geocoded.append(candidate_loc)
        return True, "ok", 16.3

    monkeypatch.setattr(ucs_location, "within_radius", fake_within_radius)
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
    import services.unified_candidate_search.location as ucs_location

    geocoded = []

    def unexpected_geocode(candidate_loc, target, miles):
        geocoded.append(candidate_loc)
        raise AssertionError("broad metro label must not be geocoded")

    monkeypatch.setattr(ucs_location, "within_radius", unexpected_geocode)
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
    import services.unified_candidate_search.location as ucs_location

    def boom(*a, **kw):
        raise AssertionError("Nominatim must not be called when all signals resolve offline")

    monkeypatch.setattr(ucs_location, "within_radius", boom)
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


def test_parse_location_short_numeric_string_is_not_recognized_as_a_zip(svc):
    """CHARACTERIZATION TEST, not a spec: documents today's backend
    behavior (which the frontend's Step 5 location input now guards
    against instead — addSourceLocation rejects a non-5-digit numeric
    value before it ever reaches a search) rather than asserting it's
    correct. A partial zip like "77" or "770" isn't recognized as a zip
    attempt at all — it's parsed as a literal city NAME, which then can't
    match any real candidate's city and silently constrains nothing. If
    _parse_location ever learns to recognize a short numeric string as a
    malformed zip instead, this test's expectations must change too."""
    for partial in ("7", "77", "770"):
        parsed = svc._parse_location(partial)
        assert parsed["zip"] == ""
        assert parsed["city"] == partial


def test_verdict_warns_once_per_unresolvable_search_zip(svc, caplog):
    """PR #859 review (Medium): the frontend's zip-lookup guard only covers
    Step 5's own input — saved/loaded job criteria, API callers, and a zip
    embedded in a longer string all reach this same resolution path, and an
    unresolvable one used to fail completely silently (every candidate
    passes the location filter unfiltered, since the search's own anchor
    point can't be placed). Not hard-failed here — that's a bigger, riskier
    change for a single bad search — but it must at least be logged."""
    import logging
    from services.unified_candidate_search import location as ucs_location

    ucs_location._WARNED_UNRESOLVABLE_SEARCH_ZIPS.discard("09999")
    candidate = {"location": "Houston, TX"}
    with caplog.at_level(logging.WARNING):
        svc._single_location_match_verdict(candidate, _criteria(location="09999", within_miles=25))
    assert any("unresolvable_search_zip" in r.message and "09999" in r.message for r in caplog.records)

    # Deduped: a second candidate against the SAME unresolvable zip doesn't
    # log again (would otherwise be one warning per row of every search
    # using it).
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        svc._single_location_match_verdict({"location": "Dallas, TX"}, _criteria(location="09999", within_miles=25))
    assert not any("unresolvable_search_zip" in r.message for r in caplog.records)


def test_verdict_unresolvable_search_zip_is_consistent_regardless_of_candidate_shape(svc):
    """Follow-up to the warning-log fix above: before this, an unresolvable
    search zip only cleanly soft-kept a candidate with a BROAD-region label
    (it fell through to broad_region_unverified). A candidate with a
    PRECISE location instead reached the Nominatim fallback and geocoded
    the same unresolvable text live — which can return a confidently wrong
    verdict (a real but unintended place) instead of an honest "can't
    tell." Both candidate shapes must now get the identical, correct
    answer with no live geocode involved."""
    criteria = _criteria(location="99999-1234", within_miles=25)

    precise = {"location": "Houston, TX"}
    assert svc._single_location_match_verdict(precise, criteria) == (
        False, "search_location_unresolvable", _UNKNOWN_DISTANCE_SENTINEL,
    )

    broad = {"location": "Dallas-Fort Worth Metroplex"}
    assert svc._single_location_match_verdict(broad, criteria) == (
        False, "search_location_unresolvable", _UNKNOWN_DISTANCE_SENTINEL,
    )


def test_verdict_zip_plus_four_resolves_normally_when_assigned(svc):
    """ZIP+4 follow-up: a well-formed, assigned ZIP+4 search still resolves
    via its 5-digit part exactly as a bare zip would — this isn't a new
    restriction, only the unassigned case above is. Relies on 77002
    (downtown Houston) being a real entry in the offline zip dataset, same
    as every other real-zip test in this file (e.g. test_zip_index_lookup_
    and_distance) — not something this test sets up itself."""
    result = svc._single_location_match_verdict(
        {"location": "Houston, TX"}, _criteria(location="77002-1234", within_miles=25)
    )
    assert result[0] is True


def test_verdict_zip_plus_four_with_an_unassigned_five_digit_part(svc):
    """ZIP+4 counterpart to test_verdict_zip_plus_four_resolves_normally_
    when_assigned: a well-formed ZIP+4 whose 5-digit part isn't an assigned
    zip (99999 is outside the real range) hits the exact same
    search_location_unresolvable path a bad bare zip does — confirmed at
    the verdict level, not just via _location_hard_gate (which covers this
    same input in test_hard_gate_soft_keeps_an_unresolvable_search_location)."""
    result = svc._single_location_match_verdict(
        {"location": "Houston, TX"}, _criteria(location="99999-1234", within_miles=25)
    )
    assert result == (False, "search_location_unresolvable", _UNKNOWN_DISTANCE_SENTINEL)


def test_verdict_embedded_unresolvable_zip_still_uses_city_state_fallback(svc):
    """A search like "Houston, TX 99999" has a real city/state alongside
    the bad zip — it must keep using that city/state fallback (unchanged
    behavior), not get swept into the new zip-is-the-only-signal
    short-circuit above."""
    result = svc._single_location_match_verdict(
        {"location": "Houston, TX"}, _criteria(location="Houston, TX 99999", within_miles=25)
    )
    assert result[0] is True
    assert result[1] != "search_location_unresolvable"


def test_hard_gate_soft_keeps_an_unresolvable_search_location(svc):
    candidate = {"location": "Houston, TX"}
    veto = svc._location_hard_gate(candidate, _criteria(location="99999-1234", within_miles=25))
    assert veto is None
    assert candidate.get("location_match_reason") == "search_location_unresolvable"
    assert candidate.get("distance_miles") is None


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
    import services.unified_candidate_search.location as ucs_location

    def unexpected_geocode(*args, **kwargs):
        raise AssertionError("broad region labels must not be treated as point locations")

    monkeypatch.setattr(ucs_location, "within_radius", unexpected_geocode)
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
