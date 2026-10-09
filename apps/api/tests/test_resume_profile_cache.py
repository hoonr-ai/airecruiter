"""Fix 4: durable parsed_resumes cache (services/resume_profile.py)."""

import asyncio

import pytest

from services import resume_profile as rp
from services.resume_profile import ResumeProfileService, normalize_resume_text, resume_sha256

RESUME = "Jane Doe\nSenior Engineer at Acme Corp 2019-present.\n" + "Python, Go, Kubernetes. " * 5


class _FakeStore:
    def __init__(self):
        self.rows = {}
        self.fail = False

    def lookup(self, sha, version, kind):
        if self.fail:
            raise RuntimeError("db down")
        return self.rows.get((sha, version, kind))

    def store(self, sha, version, kind, parsed):
        if self.fail:
            raise RuntimeError("db down")
        self.rows.setdefault((sha, version, kind), parsed)


@pytest.fixture
def store(monkeypatch):
    s = _FakeStore()
    monkeypatch.setattr(rp, "_lookup_sync", s.lookup)
    monkeypatch.setattr(rp, "_store_sync", s.store)
    monkeypatch.setattr(rp, "_settings", lambda: (True, 1))
    return s


def _counting_parser():
    calls = {"n": 0}

    async def parse(_text):
        calls["n"] += 1
        return {"job_title": "Senior Engineer", "company_experience": [{"company": "Acme"}]}

    return parse, calls


def test_normalization_makes_whitespace_variants_hash_equal():
    assert resume_sha256(RESUME) == resume_sha256("  " + RESUME.replace(" ", "   \n") + "\t")
    assert normalize_resume_text("a \n\n b\x00") == "a b"
    assert resume_sha256("too short") is None


def test_cache_hit_skips_llm(store):
    parse, calls = _counting_parser()
    first = asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse))
    second = asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse))
    assert calls["n"] == 1
    assert "_parsed_resume_cache" not in first
    assert second["_parsed_resume_cache"] == "hit"
    assert second["company_experience"] == [{"company": "Acme"}]


def test_parser_version_bump_misses(store, monkeypatch):
    parse, calls = _counting_parser()
    asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse))
    monkeypatch.setattr(rp, "_settings", lambda: (True, 2))
    asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse))
    assert calls["n"] == 2


def test_kind_is_part_of_key(store):
    parse, calls = _counting_parser()
    asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse, kind="a"))
    asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse, kind="b"))
    assert calls["n"] == 2


def test_db_error_fails_open(store):
    store.fail = True
    parse, calls = _counting_parser()
    out = asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse))
    assert out["job_title"] == "Senior Engineer"
    assert calls["n"] == 1


def test_errors_and_raw_fallbacks_not_cached(store):
    async def bad(_t):
        return {"error": "429"}

    async def raw(_t):
        return {"raw": "not json"}

    asyncio.run(ResumeProfileService.get_or_parse(RESUME, bad))
    asyncio.run(ResumeProfileService.get_or_parse(RESUME, raw))
    assert store.rows == {}


def test_disabled_flag_bypasses_cache(store, monkeypatch):
    monkeypatch.setattr(rp, "_settings", lambda: (False, 1))
    parse, calls = _counting_parser()
    asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse))
    asyncio.run(ResumeProfileService.get_or_parse(RESUME, parse))
    assert calls["n"] == 2 and store.rows == {}


def test_process_candidate_common_uses_cache(store, monkeypatch):
    """Second candidate with the same resume skips crisp + extract LLM calls."""
    from services import sourced_candidates_storage as scs

    calls = {"crisp": 0, "extract": 0}

    async def crisp(text, max_length=0):
        calls["crisp"] += 1
        return text

    async def extract(text):
        calls["extract"] += 1
        return {"job_title": "Senior Engineer", "company_experience": [{"company": "Acme", "title": "SE"}],
                "skills": [{"name": "Python"}]}

    monkeypatch.setattr(scs, "crisp_resume_with_ai", crisp)
    monkeypatch.setattr(scs, "extract_enhanced_info_with_llm", extract)
    monkeypatch.setattr(scs, "_lookup_cached_enhanced_info_by_resume_hash", lambda h: None)
    monkeypatch.setattr(scs, "save_candidate_enhanced_info", lambda *a, **k: None)

    a = asyncio.run(scs.process_jobdiva_candidate({"candidate_id": "1", "resume_text": RESUME}))
    b = asyncio.run(scs.process_jobdiva_candidate({"candidate_id": "2", "resume_text": RESUME}))
    assert calls == {"crisp": 1, "extract": 1}
    assert a["company_experience"] == b["company_experience"]
    assert a["current_title"] == b["current_title"] == "Senior Engineer"
    assert [s.get("skill") for s in a["skills"]] == [s.get("skill") for s in b["skills"]]


def test_jobdiva_cached_parse_skips_second_lookup(monkeypatch):
    from services import sourced_candidates_storage as scs

    calls = {"n": 0}

    async def _lookup(_text):
        calls["n"] += 1
        return None

    monkeypatch.setattr(scs.ResumeProfileService, "lookup", staticmethod(_lookup))
    seen = {}

    async def _fake_common(candidate, **kw):
        seen.update(kw)
        res = kw["cached_parse"] if kw["cached_parse"] is not None else await scs.ResumeProfileService.lookup("x")
        return res

    monkeypatch.setattr(scs, "_process_candidate_common", _fake_common)
    parsed = {"name": "Jane"}
    out = asyncio.run(scs.process_jobdiva_candidate({"candidate_id": "1", "resume_text": RESUME}, cached_parse=parsed))
    assert out is parsed and calls["n"] == 0
    asyncio.run(scs.process_jobdiva_candidate({"candidate_id": "1", "resume_text": RESUME}))
    assert calls["n"] == 1


def test_common_uses_cached_parse_without_lookup(monkeypatch):
    from services import sourced_candidates_storage as scs

    async def _boom(_text):
        raise AssertionError("lookup must not run when cached_parse given")

    monkeypatch.setattr(scs.ResumeProfileService, "lookup", staticmethod(_boom))

    class _Stop(Exception):
        pass

    async def _no_parse(*a, **k):
        raise _Stop

    monkeypatch.setattr(scs.ResumeProfileService, "get_or_parse", staticmethod(_no_parse))
    # A cached parse proceeds past the lookup; any later failure is fine as long
    # as it is not the lookup assertion.
    try:
        asyncio.run(scs._process_candidate_common(
            {"candidate_id": "1"}, RESUME, RESUME, "JobDiva", {}, cached_parse={"error": "x"},
        ))
    except AssertionError:
        raise
    except Exception:
        pass


def test_pipeline_trace_drops_pii_attrs(monkeypatch):
    from core import pipeline_trace as pt

    sent = []
    monkeypatch.setattr(pt, "record_custom_event", lambda name, payload: sent.append(payload))
    with pt.span_sync(
        "stage_x", batch_idx=2, n=5, scope="batch", source="JobDiva", job_id="123", ratio=0.5, ok_flag=True,
        name="Jane Doe", candidate_email="j@x.com", phone="555", resume_text="long", note="a@b.com",
        blob="x" * 500, obj={"a": 1},
    ):
        pass
    p = sent[0]
    for k in ("batch_idx", "n", "scope", "source", "ratio", "ok_flag"):
        assert k in p
    for k in ("name", "candidate_email", "phone", "resume_text", "note", "blob", "obj"):
        assert k not in p
