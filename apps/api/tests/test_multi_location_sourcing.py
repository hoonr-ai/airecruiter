import asyncio

from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch


def _service():
    return object.__new__(UnifiedCandidateSearch)


def test_location_variants_fan_out_primary_and_each_distinct_additional_location():
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        within_miles=25,
        additional_locations=[
            {"value": "Menlo Park, CA", "within_miles": 40},
            {"value": "new york, ny", "within_miles": 50},
        ],
    )

    variants = _service()._location_criteria_variants(criteria)

    assert [(item.location, item.within_miles) for item in variants] == [
        ("New York, NY", 25),
        ("Menlo Park, CA", 40),
    ]
    assert all(not item.additional_locations for item in variants)


def test_boolean_uses_or_for_multiple_locations():
    criteria = SearchCriteria(
        job_id="26-100",
        location="New York, NY",
        additional_locations=[{"value": "Menlo Park, CA", "within_miles": 25}],
    )

    boolean = _service()._build_boolean_string(criteria, dialect="generic")

    assert '"New York, NY" OR "Menlo Park, CA"' in boolean


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
        additional_locations=[{"value": "Menlo Park, CA", "within_miles": 25}],
        open_to_work=False,
    )

    result = asyncio.run(service._search_linkedin(criteria))

    assert service.unipile_service.locations == [
        "New York, NY, United States",
        "Menlo Park, CA",
    ]
    assert len(result["candidates"]) == 1
