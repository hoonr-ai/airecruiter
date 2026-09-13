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


def test_locked_question_pass_criteria_is_persisted(rubric_db):
    class Cursor:
        def __init__(self):
            self.executions = []

        def execute(self, query, params=None):
            self.executions.append((query, params))

        def fetchall(self):
            return []

    cursor = Cursor()
    criteria = "Candidate must be authorized to work in the United States."
    rubric_db._save_screen_questions_internal(
        cursor,
        "26-12345",
        [{
            "question_text": "Are you authorized to work indefinitely for any employer?",
            "pass_criteria": criteria,
            "is_default": True,
            "category": "default",
        }],
    )

    insert_params = next(
        params for query, params in cursor.executions
        if "INSERT INTO job_screen_questions" in query
    )
    assert insert_params[2] == criteria
