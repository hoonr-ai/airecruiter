"""Regression coverage for server-side core-question locks."""

import pytest

from services.job_rubric_db import JobRubricDB


@pytest.fixture
def rubric_db():
    return JobRubricDB()


def test_existing_locked_question_must_be_present_unchanged(rubric_db):
    existing = [
        {
            "question_text": (
                "Are you authorized to work indefinitely for any employer "
                "in the United States?"
            )
        }
    ]

    rubric_db._validate_locked_questions(existing, existing)

    with pytest.raises(ValueError, match="Locked screening questions"):
        rubric_db._validate_locked_questions(
            existing,
            [{"question_text": "Are you authorised to work in the US?"}],
        )


def test_locked_question_cannot_be_removed(rubric_db):
    existing = [
        {"question_text": "Will you require visa sponsorship to continue working?"}
    ]

    with pytest.raises(ValueError, match="Locked screening questions"):
        rubric_db._validate_locked_questions(existing, [])


def test_core_question_detection_is_case_insensitive(rubric_db):
    assert rubric_db._is_locked_default_question(
        "ARE YOU AUTHORIZED TO WORK INDEFINITELY FOR ANY EMPLOYER?"
    )
