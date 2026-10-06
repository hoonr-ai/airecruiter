from routers.ai_generation import _propagate_note_locations_to_rubric
from services.job_skills_extractor import JobRubric


def test_recruiter_note_locations_are_added_to_step_three_other_requirements():
    rubric = JobRubric(
        other_requirements=[
            {"value": "Location: Mountain View, CA.", "required": "Required"},
            {"value": "Night shift", "required": "Preferred"},
        ]
    )

    _propagate_note_locations_to_rubric(
        rubric,
        "Locations: Mountain View, CA; New York, NY; Phoenix, AZ 85001",
    )

    values = [item["value"] for item in rubric.other_requirements]
    assert values == [
        "Location: Mountain View, CA.",
        "Night shift",
        "Location: New York, NY.",
        "Location: Phoenix, AZ.",
    ]
    assert all(item["required"] == "Required" for item in rubric.other_requirements[2:])


def test_recruiter_note_location_propagation_is_bounded_to_ten():
    rubric = JobRubric(other_requirements=[])
    notes = "; ".join(
        f"{city}, {state}"
        for city, state in [
            ("New York", "NY"),
            ("Phoenix", "AZ"),
            ("Seattle", "WA"),
            ("Chicago", "IL"),
            ("Houston", "TX"),
            ("Miami", "FL"),
            ("Denver", "CO"),
            ("Boston", "MA"),
            ("Atlanta", "GA"),
            ("Austin", "TX"),
            ("Dallas", "TX"),
        ]
    )

    _propagate_note_locations_to_rubric(rubric, notes)

    assert len(rubric.other_requirements) == 10
