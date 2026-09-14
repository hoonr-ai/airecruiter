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
        2,
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
        2,
        "gpt-4o-mini",
        "Software Engineer",
        "What is your favorite color?",
        "criteria",
    )
    mock_parse.assert_not_called()
