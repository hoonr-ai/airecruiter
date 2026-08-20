"""Tier-2 tests: client singleton + screening / boolean / location caches.

Run with:
    cd apps/api && python -m tests.test_llm_cache_tier2
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.env_stubs import stub_required_env

stub_required_env()
os.environ.setdefault("REDIS_URL", "")


def _stub_module(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_stub_module("dotenv", load_dotenv=lambda *a, **k: None)
_stub_module("networkx")
_fake_ontology = MagicMock()
_fake_ontology.graph.nodes = [1]
_stub_module("core.graph", ontology=_fake_ontology)


def _install_fake_redis():
    store: dict = {}

    class FakeRedis:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value, ex=None):
            store[key] = value

        async def delete(self, key):
            store.pop(key, None)

    fake_asyncio = types.ModuleType("redis.asyncio")
    fake_asyncio.from_url = lambda *a, **kw: FakeRedis()
    fake_pkg = types.ModuleType("redis")
    fake_pkg.asyncio = fake_asyncio
    sys.modules["redis"] = fake_pkg
    sys.modules["redis.asyncio"] = fake_asyncio
    return store


def _reset_cache_state():
    from core import llm_cache as cache_mod
    cache_mod._redis_client = None
    cache_mod._redis_unavailable = False


def _enable_fake_redis():
    _install_fake_redis()
    from core import config as cfg
    cfg.REDIS_URL = "redis://fake"
    cfg.LLM_CACHE_ENABLED = True
    _reset_cache_state()


def test_singleton_returns_same_client_twice() -> None:
    from core import llm_client
    llm_client.reset_client_for_tests()
    c1 = llm_client.get_openai_client()
    c2 = llm_client.get_openai_client()
    assert c1 is c2, "get_openai_client must return the same instance"
    assert c1 is not None, "OPENAI_API_KEY is set, client should exist"
    print("  ok: singleton returns same instance across calls")


def test_singleton_returns_none_without_key() -> None:
    from core import llm_client, config as cfg
    saved = cfg.OPENAI_API_KEY
    cfg.OPENAI_API_KEY = ""
    llm_client.reset_client_for_tests()
    try:
        assert llm_client.get_openai_client() is None
    finally:
        cfg.OPENAI_API_KEY = saved
        llm_client.reset_client_for_tests()
    print("  ok: singleton returns None when no key")


def test_location_cache_hit_skips_openai() -> None:
    _enable_fake_redis()

    from services.location import LocationService, LocationVerdict

    verdict = LocationVerdict(
        is_within_range=True,
        distance_estimate="Same City",
        reason="Both in Brooklyn",
    )
    parsed_msg = MagicMock()
    parsed_msg.parsed = verdict
    choice = MagicMock()
    choice.message = parsed_msg
    completion = MagicMock()
    completion.choices = [choice]

    svc = LocationService()
    svc.client = MagicMock()
    svc.client.beta.chat.completions.parse = AsyncMock(return_value=completion)

    async def _go():
        v1 = await svc.check_proximity("Brooklyn, NY", "New York, NY", "on-site")
        v2 = await svc.check_proximity("Brooklyn, NY", "New York, NY", "on-site")
        # Case-insensitive cache: same loc with different casing should still hit.
        v3 = await svc.check_proximity("brooklyn, ny", "NEW YORK, NY", "on-site")
        assert v1.is_within_range is True
        assert v2.is_within_range is True
        assert v3.is_within_range is True
        assert svc.client.beta.chat.completions.parse.await_count == 1, (
            "expected 1 LLM call, got "
            f"{svc.client.beta.chat.completions.parse.await_count}"
        )

    asyncio.run(_go())
    print("  ok: location cache hit skips OpenAI on repeat lookups (case-insensitive)")


def test_screening_questions_cache_hit_skips_openai() -> None:
    _enable_fake_redis()

    # Compose a fake "raw JSON" the LLM would return.
    fake_response_payload = {
        "questions": [
            {
                "question_text": "Describe one Python project you owned end-to-end.",
                "pass_criteria": "Mentions a real project with measurable outcome.",
                "category": "technical-depth",
                "related_skill": "Python",
            }
        ]
    }

    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = '{"questions": [{"question_text": "Describe one Python project you owned end-to-end.", "pass_criteria": "Mentions a real project with measurable outcome.", "category": "technical-depth", "related_skill": "Python"}]}'

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=completion)

    from services.screening_question_generator import generate_screening_questions

    rubric = {
        "required_skills": [{"value": "Python"}],
        "preferred_skills": [],
        "job_roles": [{"value": "Software Engineer"}],
        "domain": "software",
    }

    async def _go():
        q1 = await generate_screening_questions(
            openai_client=fake_client,
            model="gpt-4o-mini",
            job_title="Backend Engineer",
            rubric=rubric,
            screening_level="medium",
        )
        q2 = await generate_screening_questions(
            openai_client=fake_client,
            model="gpt-4o-mini",
            job_title="Backend Engineer",
            rubric=rubric,
            screening_level="medium",
        )
        assert isinstance(q1, list) and len(q1) > 0, "first call should return questions"
        assert isinstance(q2, list) and len(q2) > 0, "second call should return questions"
        # The LLM should only have been invoked once across the two calls.
        assert fake_client.chat.completions.create.await_count == 1, (
            "expected 1 LLM call, got "
            f"{fake_client.chat.completions.create.await_count}"
        )

    asyncio.run(_go())
    print("  ok: screening question cache hit skips OpenAI on second call")


def main() -> None:
    tests = [
        test_singleton_returns_same_client_twice,
        test_singleton_returns_none_without_key,
        test_location_cache_hit_skips_openai,
        test_screening_questions_cache_hit_skips_openai,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            import traceback
            print(f"  FAIL {t.__name__}: {exc}")
            traceback.print_exc()
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tier-2 tests passed.")


if __name__ == "__main__":
    main()
