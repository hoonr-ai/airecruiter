"""Unit tests for the screening question generator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import screening_question_generator


def _run(coro):
    """Run async endpoint tests without requiring pytest-asyncio in CI."""
    return asyncio.run(coro)


def test_boolean_mode_sets_all_role_specific_questions_as_hard_filters(monkeypatch):
    """Ensure L0.5 (boolean) mode applies is_hard_filter=True to all role-specific questions."""
    
    mock_oai = MagicMock()
    mock_create = AsyncMock()
    mock_oai.chat.completions.create = mock_create

    # Mock the LLM to return an empty list of questions. 
    # This triggers the deterministic fallback which generates exactly `target_count` questions.
    mock_create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"questions": []}'))]
    )
    
    # Required skills will be used by the fallback
    required_skills = [{"value": "Core Java"}, {"value": "Spring Boot"}]
    
    questions = _run(
        screening_question_generator.generate_screening_questions(
            mock_oai,
            model="gpt-4",
            job_title="Software Engineer",
            rubric={"required_skills": required_skills},
            customer_name="Acme Corp",
            job_description="We write code.",
            screening_level="L0.5",
            work_arrangement="hybrid",
            city="New York",
        )
    )
    
    # First questions are front matter. Let's find the role-specific ones.
    # In boolean mode, role-specific questions are the ones starting from index 8 for hybrid,
    # but we can just filter by category != default, logistics, work-arrangement.
    role_specific_qs = [
        q for q in questions 
        if q["category"] not in ("default", "logistics", "work-arrangement")
    ]
    
    # We expect 5 role-specific questions (the target_count for L0.5)
    assert len(role_specific_qs) == 5
    
    # Assert all role-specific boolean questions are marked as hard filters
    for i, q in enumerate(role_specific_qs):
        assert q["is_hard_filter"] is True, f"Question at offset {i} was not marked as a hard filter."

