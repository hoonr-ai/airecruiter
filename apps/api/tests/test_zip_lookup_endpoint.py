"""GET /candidates/zip-lookup — lets the Step 5 location input confirm or
reject a zip before it's added to search criteria.

QA regression: typing an unresolvable zip (e.g. "99999", outside the real
US range) into the Step 5 location field silently returned candidates
instead of erroring or filtering — because the search's own anchor point
then failed to geocode, which made every candidate pass the location
filter unfiltered (see services/unified_candidate_search/location.py's
_single_location_match_verdict: an unresolvable required["zip"] leaves
required_point as None for the whole function, so no candidate's offline
distance can ever be computed and every row falls through to the
geocode_unavailable soft-keep). This endpoint lets the frontend catch the
typo at input time instead.
"""
import pytest

from core.auth import UserIdentity
from routers.candidates import lookup_zip_code


@pytest.mark.anyio
async def test_zip_lookup_resolves_a_real_zip():
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    res = await lookup_zip_code(zip="77002", user=user)
    assert res["status"] == "success"
    assert res["valid"] is True
    assert res["city"] == "Houston"
    assert res["state"] == "TX"


@pytest.mark.anyio
async def test_zip_lookup_rejects_an_out_of_range_zip():
    """99999 is outside the real US zip range — the exact QA repro."""
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    res = await lookup_zip_code(zip="99999", user=user)
    assert res["status"] == "success"
    assert res["valid"] is False
    assert "city" not in res


@pytest.mark.anyio
async def test_zip_lookup_rejects_non_zip_text():
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    res = await lookup_zip_code(zip="not-a-zip", user=user)
    assert res["valid"] is False
