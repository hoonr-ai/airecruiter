import asyncio
from routers.campaigns import _seed_job_rubric

def test_seed_job_rubric_category_assignment(monkeypatch):
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
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    # Return a fake DB row so the unpacking succeeds
    mock_cur.fetchone.return_value = (
        "test-ref", "Test Title", "Desc", "City", "Onsite", "L1", "Enhanced", "Notes", "AI Desc", "NY", "Acme", "1234"
    )
    monkeypatch.setattr("routers._helpers.get_db_connection", lambda: mock_conn)
    monkeypatch.setattr("core.llm_client.get_openai_client", MagicMock())
    
    from dataclasses import dataclass
    @dataclass
    class DummyRubric:
        required_skills: list
        
    mock_extractor = MagicMock()
    mock_extractor.return_value.extract_full_rubric = AsyncMock(return_value=DummyRubric(required_skills=[]))
    monkeypatch.setattr("services.job_skills_extractor.JobSkillsExtractor", mock_extractor)
    
    asyncio.run(_seed_job_rubric(
        campaign={"id": "test", "recruiter_notes": "notes", "screening_level": "L1", "job_title": "Test", "job_description": "Testing", "city": "NYC", "rubric": {}},
        ref="test-ref",
        bot_introduction="Hi"
    ))
    
    assert mock_save.called
    saved_rubric = mock_save.call_args.kwargs["rubric_obj"]
    saved_questions = saved_rubric["screen_questions"]
    
    role_specific = [q for q in saved_questions if str(q.get("category", "")) == "role-specific"]
    
    # We should have the non-excluded dict question updated to 'role-specific'
    assert len(role_specific) == 1
    assert role_specific[0]["question_text"] == "Q1"
