import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from apps.api.routers.ai_generation import moderate_screening_questions, ModerateQuestionsRequest, ModerateQuestionItem
from apps.api.core.auth import UserIdentity

@pytest.mark.asyncio
async def test_expected_answer_moderation():
    with patch("apps.api.routers.ai_generation.get_openai_client") as mock_get_client, \
         patch("apps.api.routers.ai_generation._llm_cache") as mock_cache:
        
        # Mock the cache to always miss
        mock_cache.get_json = AsyncMock(return_value=None)
        mock_cache.make_key = MagicMock(return_value="mock_key")
        mock_cache.set_json = AsyncMock()

        # Mock the OpenAI client
        mock_oai = MagicMock()
        mock_parse = AsyncMock()
        mock_oai.beta.chat.completions.parse = mock_parse
        mock_get_client.return_value = mock_oai
        
        mock_verdict = MagicMock()
        mock_verdict.model_dump.return_value = {
            "ok": False,
            "flags": ["unsafe", "grammatical_error"],
            "reason": "This is unsafe."
        }
        mock_message = MagicMock()
        mock_message.parsed = mock_verdict
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_parse.return_value = MagicMock(choices=[mock_choice])

        req = ModerateQuestionsRequest(
            questions=[
                ModerateQuestionItem(
                    key="test_key",
                    question_text="What is your favorite color?",
                    expected_answer="Union affiliation"
                )
            ],
            job_title="Software Engineer"
        )
        
        user = UserIdentity(email="test@example.com", id="1")
        
        res = await moderate_screening_questions(req, user)
        
        assert res["status"] == "success"
        assert len(res["results"]) == 1
        result = res["results"][0]
        
        assert result["ok"] is False
        assert "unsafe" in result["flags"]
        assert "grammatical_error" in result["flags"]
        
        # Ensure that make_key was called with the expected answer
        mock_cache.make_key.assert_called_with(
            "q_moderation", 2, "gpt-4o-mini", "Software Engineer", 
            "What is your favorite color?", "Union affiliation"
        )
        
        # Verify that expected_answer was included in the LLM prompt
        call_args = mock_parse.call_args
        messages = call_args.kwargs["messages"]
        user_message = messages[1]["content"]
        assert "Expected answer/Pass criteria:" in user_message
        assert "Union affiliation" in user_message

@pytest.mark.asyncio
async def test_moderation_cache_hit():
    with patch("apps.api.routers.ai_generation.get_openai_client") as mock_get_client, \
         patch("apps.api.routers.ai_generation._llm_cache") as mock_cache:
        
        # Mock the cache to hit
        mock_cache.get_json = AsyncMock(return_value={
            "ok": False,
            "flags": ["nsfw"],
            "reason": "Bad word."
        })
        mock_cache.make_key = MagicMock(return_value="mock_key")

        # Mock the OpenAI client (should not be called)
        mock_oai = MagicMock()
        mock_parse = AsyncMock()
        mock_oai.beta.chat.completions.parse = mock_parse
        mock_get_client.return_value = mock_oai
        
        req = ModerateQuestionsRequest(
            questions=[
                ModerateQuestionItem(
                    key="test_key",
                    question_text="Bad question",
                    expected_answer=""
                )
            ],
            job_title="Test"
        )
        
        user = UserIdentity(email="test@example.com", id="1")
        
        res = await moderate_screening_questions(req, user)
        
        assert res["status"] == "success"
        result = res["results"][0]
        assert result["ok"] is False
        assert result["flags"] == ["nsfw"]
        
        # Ensure LLM was not called
        mock_parse.assert_not_called()
