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


def test_get_article_for_title():
    """Verify phonetic article selection for job titles."""
    from routers._helpers import get_article_for_title

    # Vowel sounds (an)
    assert get_article_for_title("Accountant") == "an"
    assert get_article_for_title("HR Manager") == "an"
    assert get_article_for_title("SRE Lead") == "an"
    assert get_article_for_title("AWS Engineer") == "an"
    assert get_article_for_title("MBA Graduate") == "an"
    assert get_article_for_title("IT Specialist") == "an"
    assert get_article_for_title("Executive Director") == "an"

    # Consonant sounds (a) including 'yoo' sounds like UX, UI
    assert get_article_for_title("Java Developer") == "a"
    assert get_article_for_title("UX Designer") == "a"
    assert get_article_for_title("UI Developer") == "a"
    assert get_article_for_title("Software Engineer") == "a"


def test_is_legacy_default_bot_intro():
    """Verify legacy bot intro detection requires all signature phrases."""
    from routers._helpers import is_legacy_default_bot_intro

    legacy_intro = (
        "Hi {{candidate name}}, I'm Alex, a virtual recruiter with Pyramid Consulting. "
        "We are helping our client recruit for a Java Developer in New York, NY, and you seem to be a good fit for the role. "
        "Please note that conversation may be recorded for verification and quality purposes. "
        "Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?"
    )
    assert is_legacy_default_bot_intro(legacy_intro) is True

    # Recruiter typed custom intro containing only 1 phrase — should NOT be flagged as legacy default
    custom_intro = "Hi, this interview takes about 8-12 minutes. Thanks for applying!"
    assert is_legacy_default_bot_intro(custom_intro) is False


def test_normalize_bot_intro_tokens():
    """Verify placeholder token substitution in bot intros."""
    from routers._helpers import normalize_bot_intro_tokens

    template = "Hi {name}, we are recruiting for {article} {title} in {location}."
    result = normalize_bot_intro_tokens(template, job_title="HR Manager", job_location="Chicago, IL")
    assert result == "Hi {{candidate name}}, we are recruiting for an HR Manager in Chicago, IL."

    template_consonant = "Hi {{candidate name}}, we are recruiting for {{article}} {{job_title}} in {{job_location}}."
    result_consonant = normalize_bot_intro_tokens(template_consonant, job_title="Java Engineer", job_location="Austin, TX")
    assert result_consonant == "Hi {{candidate name}}, we are recruiting for a Java Engineer in Austin, TX."

