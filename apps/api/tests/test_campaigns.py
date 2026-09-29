import pytest
from routers.campaigns import _seed_job_rubric

@pytest.mark.asyncio
async def test_seed_job_rubric_category_assignment(monkeypatch):
    """Ensure _seed_job_rubric correctly assigns 'role-specific' category to generated technical questions."""
    from unittest.mock import MagicMock, AsyncMock
    
    # Mock dependencies
    mock_campaign = {"recruiter_notes": "test notes"}
    mock_rubric = {}
    
    mock_ai = AsyncMock()
    # Return two questions from generator, one dict, one string (malformed LLM output)
    mock_ai.return_value = [
        {"question_text": "Q1", "category": "custom"},
        {"question_text": "Q2", "category": "intro"},  # Should be excluded
        "malformed_string_question"
    ]
    monkeypatch.setattr("services.screening_question_generator.generate_screening_questions", mock_ai)
    
    mock_db = MagicMock()
    mock_save = MagicMock()
    mock_db.return_value.save_full_rubric = mock_save
    monkeypatch.setattr("services.job_rubric_db.JobRubricDB", mock_db)
    monkeypatch.setattr("routers._helpers.get_db_connection", MagicMock())
    monkeypatch.setattr("core.llm_client.get_openai_client", MagicMock())
    monkeypatch.setattr("services.job_skills_extractor.JobSkillsExtractor", MagicMock())
    
    await _seed_job_rubric(
        campaign={"id": "test", "recruiter_notes": "notes", "screening_level": "L1", "job_title": "Test", "job_description": "Testing", "city": "NYC", "rubric": {}},
        ref="test-ref",
        bot_introduction="Hi"
    )
    
    assert mock_save.called
    saved_rubric = mock_save.call_args.kwargs["rubric_obj"]
    saved_questions = saved_rubric["screen_questions"]
    
    # We should have the non-excluded dict question updated to 'role-specific'
    assert len(saved_questions) == 1
    assert saved_questions[0]["question_text"] == "Q1"
    assert saved_questions[0]["category"] == "role-specific"
