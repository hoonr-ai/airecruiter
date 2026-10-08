"""Tier-3 tests: model_for env override + Redis-backed embedding L2.

Run with:
    cd apps/api && python -m tests.test_llm_cache_tier3
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

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


def _install_fake_redis():
    store: dict = {}
    ttls: dict = {}

    class FakeRedis:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value, ex=None):
            store[key] = value
            ttls[key] = ex

        async def delete(self, key):
            store.pop(key, None)

    fake_asyncio = types.ModuleType("redis.asyncio")
    fake_asyncio.from_url = lambda *a, **kw: FakeRedis()
    fake_pkg = types.ModuleType("redis")
    fake_pkg.asyncio = fake_asyncio
    sys.modules["redis"] = fake_pkg
    sys.modules["redis.asyncio"] = fake_asyncio
    store["__ttls__"] = ttls
    return store


def _reset_cache_state():
    from core import llm_cache as cache_mod
    cache_mod._redis_client = None
    cache_mod._redis_unavailable = False


def _enable_fake_redis():
    store = _install_fake_redis()
    from core import config as cfg
    cfg.REDIS_URL = "redis://fake"
    cfg.LLM_CACHE_ENABLED = True
    _reset_cache_state()
    return store


def test_model_for_honors_env_override() -> None:
    from core.llm_client import model_for
    assert model_for("boolean", "gpt-4.1-nano") == "gpt-4.1-nano"
    os.environ["LLM_MODEL_BOOLEAN"] = "gpt-4o-mini"
    try:
        assert model_for("boolean", "gpt-4.1-nano") == "gpt-4o-mini"
    finally:
        del os.environ["LLM_MODEL_BOOLEAN"]
    print("  ok: model_for honors LLM_MODEL_<PURPOSE> env override")


def test_embedding_l2_redis_serves_subsequent_workers() -> None:
    """Simulate a worker restart: warm a term, drop the L1 cache, warm
    again. The second warm must NOT call OpenAI — it should pull the
    vector from the Redis L2."""
    _enable_fake_redis()
    from services import skill_embeddings

    # Drop L1 to simulate process boundaries / restarts.
    skill_embeddings._CACHE.clear()

    fake_resp = MagicMock()
    fake_resp.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]
    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock(return_value=fake_resp)

    async def _go():
        # First worker boot: nothing in L1 or L2 → OpenAI gets called.
        from unittest.mock import patch
        with patch("services.skill_embeddings._client", return_value=fake_client):
            await skill_embeddings.warm_terms(["python"])
        assert fake_client.embeddings.create.await_count == 1, (
            "first warm should call OpenAI"
        )
        assert "python" in skill_embeddings._CACHE

        # Simulate worker restart: L1 lost, L2 retained.
        skill_embeddings._CACHE.clear()
        fake_client.embeddings.create.reset_mock()

        with patch("services.skill_embeddings._client", return_value=fake_client):
            await skill_embeddings.warm_terms(["python"])
        assert fake_client.embeddings.create.await_count == 0, (
            "second warm should have been served from Redis L2"
        )
        got = skill_embeddings._CACHE.get("python")
        assert got and all(abs(a - b) < 1e-6 for a, b in zip(got, [0.1, 0.2, 0.3])), (
            "L1 should be repopulated from L2"
        )

    asyncio.run(_go())
    print("  ok: embedding L2 (Redis) survives L1 cache clear → no extra OpenAI calls")


def test_embedding_l2_writes_with_ttl_as_float32() -> None:
    from unittest.mock import patch
    import base64

    store = _enable_fake_redis()
    from services import skill_embeddings

    skill_embeddings._CACHE.clear()
    fake_resp = MagicMock()
    fake_resp.data = [MagicMock(embedding=[0.5] * 1536)]
    fake_client = MagicMock()
    fake_client.embeddings.create = AsyncMock(return_value=fake_resp)

    async def _go():
        with patch("services.skill_embeddings._client", return_value=fake_client):
            await skill_embeddings.warm_terms(["golang"])

    asyncio.run(_go())
    key = skill_embeddings._embed_redis_key("golang")
    assert key.startswith("llm:embed:v2:"), key
    assert store["__ttls__"][key] == skill_embeddings._EMBED_REDIS_TTL_S
    assert len(base64.b64decode(store[key])) == 1536 * 4
    assert skill_embeddings._CACHE["golang"].typecode == "f", "L1 should hold float32 arrays"
    print("  ok: embedding L2 writes float32 with a TTL")


def main() -> None:
    tests = [
        test_model_for_honors_env_override,
        test_embedding_l2_redis_serves_subsequent_workers,
        test_embedding_l2_writes_with_ttl_as_float32,
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
    print(f"\nAll {len(tests)} tier-3 tests passed.")


if __name__ == "__main__":
    main()
