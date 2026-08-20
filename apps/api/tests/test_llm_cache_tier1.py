"""Tier-1 LLM cache tests.

Covers:
- llm_cache no-ops cleanly when REDIS_URL is empty
- make_key / hash_inputs are deterministic and isolate distinct inputs
- TribunalService.evaluate_narrative reads cached verdicts and skips the LLM
- AIService._extract_candidate reads cached profiles and skips the LLM

Follows the same standalone-script pattern as test_exa_query_builder.py.
Run with:
    cd apps/api && python -m tests.test_llm_cache_tier1
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
# Force cache off for the "no Redis" test and on (mocked) for the others.
os.environ.setdefault("REDIS_URL", "")


def _stub_module(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_stub_module("dotenv", load_dotenv=lambda *a, **k: None)
# Stub the heavy deps that the ai_service module-singleton drags in. The
# test creates AIService instances directly via object.__new__, so the
# real ontology / graph layer never runs.
_stub_module("networkx")
_fake_ontology = MagicMock()
_fake_ontology.graph.nodes = [1]  # non-empty so load_from_db isn't called
_stub_module("core.graph", ontology=_fake_ontology)


def _reset_cache_state():
    """Drop the cached redis client + 'unavailable' latch so each test can
    re-evaluate REDIS_URL / LLM_CACHE_ENABLED freshly."""
    from core import llm_cache as cache_mod
    cache_mod._redis_client = None
    cache_mod._redis_unavailable = False


def test_make_key_is_deterministic_and_distinct() -> None:
    from core import llm_cache

    k1 = llm_cache.make_key("foo", 1, "abc", 42, None)
    k2 = llm_cache.make_key("foo", 1, "abc", 42, None)
    k3 = llm_cache.make_key("foo", 1, "abc", 43, None)
    k4 = llm_cache.make_key("foo", 2, "abc", 42, None)

    assert k1 == k2, "same inputs must yield same key"
    assert k1 != k3, "different input must yield different key"
    assert k1 != k4, "version bump must yield different key"
    assert k1.startswith("llm:foo:v1:"), f"unexpected key format: {k1}"
    print("  ok: make_key deterministic + distinct + namespaced")


def test_no_redis_url_makes_cache_a_clean_noop() -> None:
    """When REDIS_URL is empty, get_json/set_json return None / swallow.
    Nothing should raise, and is_enabled() must report False."""
    _reset_cache_state()
    from core import config as cfg
    cfg.REDIS_URL = ""  # force-empty for this test
    _reset_cache_state()

    from core import llm_cache

    async def _go():
        assert await llm_cache.get_json("anything") is None
        await llm_cache.set_json("anything", {"a": 1}, ttl_seconds=60)
        assert llm_cache.is_enabled() is False

    asyncio.run(_go())
    print("  ok: cache is a clean no-op when REDIS_URL is empty")


def _install_fake_redis():
    """Install a fake redis.asyncio with a single in-memory dict so we can
    drive get/set/delete deterministically."""
    store: dict = {}

    class FakeRedis:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value, ex=None):
            store[key] = value

        async def delete(self, key):
            store.pop(key, None)

    fake_redis_asyncio = types.ModuleType("redis.asyncio")
    fake_redis_asyncio.from_url = lambda *a, **kw: FakeRedis()
    fake_redis_pkg = types.ModuleType("redis")
    fake_redis_pkg.asyncio = fake_redis_asyncio
    sys.modules["redis"] = fake_redis_pkg
    sys.modules["redis.asyncio"] = fake_redis_asyncio
    return store


def test_tribunal_cache_hit_skips_openai() -> None:
    """Second evaluate_narrative call on identical inputs must NOT hit the
    OpenAI client."""
    _install_fake_redis()
    from core import config as cfg
    cfg.REDIS_URL = "redis://fake"
    cfg.LLM_CACHE_ENABLED = True
    _reset_cache_state()

    from core.intelligence import TribunalVerdict, CareerTrajectory
    from services.tribunal import TribunalService

    verdict = TribunalVerdict(
        skeptic_summary="ok",
        advocate_summary="ok",
        consensus_flags=[],
        consensus_strengths=[],
        trajectory_analysis=CareerTrajectory(direction="stable", reasoning="r"),
        narrative_tag="solid_performer",
    )

    parsed_msg = MagicMock()
    parsed_msg.parsed = verdict
    choice = MagicMock()
    choice.message = parsed_msg
    completion = MagicMock()
    completion.choices = [choice]

    mock_client = MagicMock()
    mock_client.beta.chat.completions.parse = AsyncMock(return_value=completion)

    svc = TribunalService()
    svc.client = mock_client

    # Use minimal real models so encode() works.
    from core.models import CandidateProfile, CandidateMetadata, ComputedCandidateStats
    from core.models import JobDescription, JobMetadata, GatingRules, SenioritySignals

    cand = CandidateProfile(
        id="c1",
        candidate_metadata=CandidateMetadata(name="Test"),
        computed_stats=ComputedCandidateStats(),
        is_valid=True,
    )
    jd = JobDescription(
        id="j1",
        job_metadata=JobMetadata(title="Eng"),
        gating_rules=GatingRules(),
        requirements=[],
        seniority_signals=SenioritySignals(),
        is_valid=True,
    )

    resume_text = "Resume content that is long enough to be meaningful " * 20

    async def _go():
        v1 = await svc.evaluate_narrative(resume_text, cand, jd)
        v2 = await svc.evaluate_narrative(resume_text, cand, jd)
        assert v1.narrative_tag == "solid_performer", f"v1 tag was {v1.narrative_tag}"
        assert v2.narrative_tag == "solid_performer", f"v2 tag was {v2.narrative_tag}"
        # The mock should have been invoked exactly once.
        assert mock_client.beta.chat.completions.parse.await_count == 1, (
            f"expected 1 LLM call, got {mock_client.beta.chat.completions.parse.await_count}"
        )

    asyncio.run(_go())
    print("  ok: tribunal cache hit skips OpenAI on second call")


def test_candidate_parse_cache_hit_skips_openai() -> None:
    """Second _extract_candidate call on identical resume text must NOT
    hit the OpenAI client."""
    _install_fake_redis()
    from core import config as cfg
    cfg.REDIS_URL = "redis://fake"
    cfg.LLM_CACHE_ENABLED = True
    _reset_cache_state()

    # Build a real CandidateProfile via the model to ensure the
    # round-trip (dump -> set -> get -> validate) works on the schema.
    from core.models import CandidateProfile, CandidateMetadata, ComputedCandidateStats

    profile = CandidateProfile(
        id="placeholder",
        candidate_metadata=CandidateMetadata(name="Alice"),
        computed_stats=ComputedCandidateStats(),
        is_valid=True,
    )

    parsed_msg = MagicMock()
    parsed_msg.parsed = profile
    choice = MagicMock()
    choice.message = parsed_msg
    completion = MagicMock()
    completion.choices = [choice]

    # Avoid loading the heavy AIService.__init__ (graph load, ontology, etc.).
    from services import ai_service as ai_svc_mod
    svc = object.__new__(ai_svc_mod.AIService)
    svc.client = MagicMock()
    svc.client.beta.chat.completions.parse = AsyncMock(return_value=completion)

    # Bypass the Azure-Agent branch for this test.
    with patch.object(ai_svc_mod, "AZURE_AGENT_AVAILABLE", False), \
         patch.object(ai_svc_mod, "_azure_agent", None):
        resume_text = "John Doe — Senior Engineer at Acme. " * 50

        async def _go():
            p1 = await svc._extract_candidate(resume_text, "cand-1")
            p2 = await svc._extract_candidate(resume_text, "cand-2")
            assert p1.candidate_metadata.name == "Alice"
            # cid is per-call: the cached version should adopt cid-2.
            assert p2.id == "cand-2", f"cached profile should adopt new cid, got {p2.id}"
            assert svc.client.beta.chat.completions.parse.await_count == 1, (
                "expected 1 LLM call, got "
                f"{svc.client.beta.chat.completions.parse.await_count}"
            )

        asyncio.run(_go())
    print("  ok: candidate parse cache hit skips OpenAI on second call")


def main() -> None:
    tests = [
        test_make_key_is_deterministic_and_distinct,
        test_no_redis_url_makes_cache_a_clean_noop,
        test_tribunal_cache_hit_skips_openai,
        test_candidate_parse_cache_hit_skips_openai,
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
    print(f"\nAll {len(tests)} tier-1 cache tests passed.")


if __name__ == "__main__":
    main()
