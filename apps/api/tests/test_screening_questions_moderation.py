"""Regression coverage for screening-question moderation of pass criteria."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from core import llm_cache
from core.auth import UserIdentity
from routers import ai_generation


def _run(coro):
    """Run async endpoint tests without requiring pytest-asyncio in CI."""
    return asyncio.run(coro)


def _request(expected_answer="Union affiliation"):
    return ai_generation.ModerateQuestionsRequest(
        questions=[
            ai_generation.ModerateQuestionItem(
                key="test_key",
                question_text="What is your favorite color?",
                expected_answer=expected_answer,
            )
        ],
        job_title="Software Engineer",
    )


def _user():
    return UserIdentity(email="test@example.com", role="recruiter")


def test_expected_answer_is_moderated_and_part_of_the_cache_key(monkeypatch):
    mock_oai = MagicMock()
    mock_parse = AsyncMock()
    mock_oai.beta.chat.completions.parse = mock_parse
    monkeypatch.setattr(ai_generation, "get_openai_client", lambda: mock_oai)

    make_key = MagicMock(return_value="mock_key")
    monkeypatch.setattr(llm_cache, "make_key", make_key)
    monkeypatch.setattr(llm_cache, "get_json", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_cache, "set_json", AsyncMock())

    verdict = MagicMock()
    verdict.model_dump.return_value = {
        "ok": False,
        "flags": ["unsafe", "grammatical_error"],
        "reason": "This is unsafe.",
    }
    mock_parse.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(parsed=verdict))]
    )

    result = _run(ai_generation.moderate_screening_questions(_request(), _user()))

    assert result["status"] == "success"
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["flags"] == ["unsafe", "grammatical_error"]
    make_key.assert_called_once_with(
        "q_moderation",
        3,
        "gpt-4o-mini",
        "Software Engineer",
        "What is your favorite color?",
        "Union affiliation",
    )
    user_message = mock_parse.call_args.kwargs["messages"][1]["content"]
    assert "Expected answer/Pass criteria:\nUnion affiliation" in user_message


def test_cache_hit_uses_expected_answer_key_and_skips_the_llm(monkeypatch):
    mock_oai = MagicMock()
    mock_parse = AsyncMock()
    mock_oai.beta.chat.completions.parse = mock_parse
    monkeypatch.setattr(ai_generation, "get_openai_client", lambda: mock_oai)

    make_key = MagicMock(return_value="mock_key")
    monkeypatch.setattr(llm_cache, "make_key", make_key)
    monkeypatch.setattr(
        llm_cache,
        "get_json",
        AsyncMock(return_value={"ok": False, "flags": ["nsfw"], "reason": "Bad word."}),
    )

    result = _run(ai_generation.moderate_screening_questions(_request("  criteria  "), _user()))

    assert result["status"] == "success"
    assert result["results"][0]["flags"] == ["nsfw"]
    make_key.assert_called_once_with(
        "q_moderation",
        3,
        "gpt-4o-mini",
        "Software Engineer",
        "What is your favorite color?",
        "criteria",
    )
    mock_parse.assert_not_called()


def test_deterministic_grammar_checks_cover_common_recruiter_question_errors():
    cases = {
        "How many years experience you have?": (
            "How many years of experience",
            "How many years of experience do you have?",
        ),
        "How much years of experience do you have?": (
            "How many years",
            "How many years of experience do you have?",
        ),
        "What is your current designation currently?": (
            "repeated",
            "What is your current designation?",
        ),
        "Is you open to work overtime?": (
            "Are you",
            "Are you open to work overtime?",
        ),
    }

    for question, (expected_reason, expected_corrected) in cases.items():
        fix = ai_generation._deterministic_grammar_fix(question)
        assert fix is not None, question
        reason, corrected = fix
        assert expected_reason in reason
        assert corrected == expected_corrected


def test_deterministic_grammar_flag_overrides_an_llm_ok_verdict(monkeypatch):
    mock_oai = MagicMock()
    mock_parse = AsyncMock()
    mock_oai.beta.chat.completions.parse = mock_parse
    monkeypatch.setattr(ai_generation, "get_openai_client", lambda: mock_oai)
    monkeypatch.setattr(llm_cache, "make_key", MagicMock(return_value="grammar_key"))
    monkeypatch.setattr(llm_cache, "get_json", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_cache, "set_json", AsyncMock())

    verdict = MagicMock()
    verdict.model_dump.return_value = {"ok": True, "flags": [], "reason": ""}
    mock_parse.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(parsed=verdict))]
    )
    req = ai_generation.ModerateQuestionsRequest(
        questions=[ai_generation.ModerateQuestionItem(
            key="grammar_key", question_text="How much years of experience do you have?"
        )]
    )

    result = _run(ai_generation.moderate_screening_questions(req, _user()))

    assert result["results"][0] == {
        "key": "grammar_key",
        "question_text": "How much years of experience do you have?",
        "ok": False,
        "flags": ["grammatical_error"],
        "reason": "Use ‘How many years’ rather than ‘How much years.’",
        "corrected_question": "How many years of experience do you have?",
        "checked": True,
    }
