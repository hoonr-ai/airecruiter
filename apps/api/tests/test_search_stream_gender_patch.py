"""Search stream must not gate rows on AI gender inference (2026-08-20).

The /candidates/search NDJSON stream used to await one OpenAI name→gender
call per candidate event before yielding it, serializing every fresh-name
burst (~1s+ per row, 8s timeout) — the quick-first JobAgent tranche would
have painted seconds later than it arrived. Rows now stream immediately
with the heuristic gender fields; a confident AI result follows as a
targeted ``candidate_detail`` patch with ``stage: "gender"``.

These tests drive the real endpoint function with the search generator and
the AI inference stubbed, and pin:
  - candidate rows carry heuristic ("default") gender at emit time — proof
    they did not wait for the AI call
  - each confident AI result arrives as a stage="gender" patch after the
    row, carrying male/female only
  - unconfident AI results produce no patch (a "default" must never
    overwrite the streamed heuristic fields)
"""
import asyncio
import json
import os

for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY",
):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import routers.candidates as rc  # noqa: E402
from core.auth import UserIdentity  # noqa: E402
from models import CandidateSearchRequest  # noqa: E402
from services.gender_logic import normalize_gender_prediction  # noqa: E402


def _request():
    return CandidateSearchRequest(
        job_id="test-job-1",
        sources=["JobDiva-JobAgent"],
        location_type="Onsite",   # skips the monitored_jobs DB read
        client_name="Acme",       # skips the customer_name DB read
    )


def _user():
    return UserIdentity(email="qa@hoonr.ai", role="admin")


def _fake_search_events(n_candidates):
    async def fake_search(criteria):
        yield {"type": "stage", "data": "Searching JobDiva (JobAgent)..."}
        for i in range(n_candidates):
            yield {
                "type": "candidate",
                "data": {
                    "candidate_id": str(i),
                    "id": str(i),
                    "name": f"Pat Quick{i}",
                    "source": "JobDiva-JobAgent",
                    "_stage": "agent_result",
                    "resume_text": "resume body",
                },
            }
        yield {"type": "summary", "data": {"summary": {}}}

    return fake_search


def _collect_stream(request, user):
    async def _run():
        response = await rc.search_jobdiva_candidates(request, user)
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
            for line in text.split("\n"):
                if line.strip():
                    events.append(json.loads(line))
        return events

    return asyncio.run(_run())


def test_rows_stream_with_heuristic_gender_and_ai_arrives_as_patch(monkeypatch):
    monkeypatch.setattr(rc, "_verify_job_access_by_id", lambda job_id, user: None)
    monkeypatch.setattr(
        rc.unified_search_service, "search_candidates", _fake_search_events(3)
    )

    async def fake_infer(name, threshold=0.6):
        await asyncio.sleep(0.05)  # slower than the whole fake search
        return normalize_gender_prediction(
            predicted_label="female",
            confidence=0.95,
            source="inferred_ai_name",
            threshold=threshold,
        )

    monkeypatch.setattr(rc, "infer_gender_from_name_ai", fake_infer)

    events = _collect_stream(_request(), _user())

    rows = [e for e in events if e.get("type") == "candidate"]
    gender_patches = [
        e for e in events
        if e.get("type") == "candidate_detail" and e.get("stage") == "gender"
    ]
    assert len(rows) == 3
    # Rows did NOT wait for the (slow) AI call: they carry the heuristic
    # default at emit time.
    assert all(r["data"].get("gender_label") == "default" for r in rows)
    # Every row's confident AI result followed as a gender patch.
    assert {p["candidate_id"] for p in gender_patches} == {"0", "1", "2"}
    assert all(p["patch"]["gender_label"] == "female" for p in gender_patches)
    # Causality: each patch appears after its row.
    for p in gender_patches:
        row_idx = next(
            i for i, e in enumerate(events)
            if e.get("type") == "candidate"
            and str(e["data"]["candidate_id"]) == p["candidate_id"]
        )
        assert events.index(p) > row_idx


def test_unconfident_ai_result_produces_no_patch(monkeypatch):
    monkeypatch.setattr(rc, "_verify_job_access_by_id", lambda job_id, user: None)
    monkeypatch.setattr(
        rc.unified_search_service, "search_candidates", _fake_search_events(2)
    )

    async def fake_infer(name, threshold=0.6):
        return normalize_gender_prediction(
            predicted_label="default",
            confidence=0.0,
            source="inferred_ai_name",
            threshold=threshold,
        )

    monkeypatch.setattr(rc, "infer_gender_from_name_ai", fake_infer)

    events = _collect_stream(_request(), _user())

    rows = [e for e in events if e.get("type") == "candidate"]
    gender_patches = [
        e for e in events
        if e.get("type") == "candidate_detail" and e.get("stage") == "gender"
    ]
    assert len(rows) == 2
    assert gender_patches == []
    # The stream still completes normally with the summary event.
    assert any(e.get("type") == "summary" for e in events)
