"""Phase 3 of the employer-identification work (2026-09-02):

  - resume freshness: launch candidates whose employer evidence rests on a
    years-old resume classify as "verified_stale" (advisory, never blocking);
  - person-wide propagation: a stated interview answer is stamped on every
    sourced_candidates row of that candidate, not just the interviewed job's.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

from services.employer_resolution import (
    _parse_resume_ts,
    employer_verification_state,
    resume_is_stale,
    stamp_resume_freshness,
)
from services.stated_employer import EMPLOYER_QUESTION_TEXT


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


VERIFIED = {"company_experience": [{"company": "Acme Corp", "end_date": "Present"}]}


# ── staleness classification ───────────────────────────────────────────────

def test_parse_resume_ts_shapes():
    assert _parse_resume_ts("2026-06-18T02:07:36") is not None
    assert _parse_resume_ts("2026-06-18") is not None
    assert _parse_resume_ts("06/18/2026") is None
    assert _parse_resume_ts("") is None
    assert _parse_resume_ts(None) is None


def test_fresh_resume_stays_verified():
    cand = dict(VERIFIED, candidate_id="1", resume_updated_at=_iso_days_ago(30))
    assert resume_is_stale(cand) is False
    assert employer_verification_state(cand) == "verified"


def test_old_resume_downgrades_to_verified_stale():
    cand = dict(VERIFIED, candidate_id="1", resume_updated_at=_iso_days_ago(900))
    assert resume_is_stale(cand) is True
    assert employer_verification_state(cand) == "verified_stale"


def test_missing_resume_date_never_downgrades():
    cand = dict(VERIFIED, candidate_id="1")
    assert resume_is_stale(cand) is False
    assert employer_verification_state(cand) == "verified"


def test_stated_answer_is_never_resume_stale():
    cand = dict(
        VERIFIED,
        candidate_id="1",
        resume_updated_at=_iso_days_ago(900),
        stated_current_employer="Cognizant",
    )
    assert employer_verification_state(cand) == "verified"


def test_staleness_knob_zero_disables(monkeypatch):
    from core import sourcing_config
    monkeypatch.setattr(sourcing_config, "EMPLOYER_STALE_RESUME_MONTHS", 0, raising=False)
    cand = dict(VERIFIED, candidate_id="1", resume_updated_at=_iso_days_ago(900))
    assert resume_is_stale(cand) is False
    assert employer_verification_state(cand) == "verified"


def test_resume_date_reads_nested_data_blob():
    cand = {
        "candidate_id": "1",
        "data": dict(VERIFIED, resume_updated_at=_iso_days_ago(900)),
    }
    assert employer_verification_state(cand) == "verified_stale"


# ── stamp_resume_freshness targeting ───────────────────────────────────────

class _FakeDates:
    def __init__(self, dates):
        self.dates = dates
        self.calls: List[List[str]] = []

    async def fetch_resume_dates(self, ids):
        self.calls.append(list(ids))
        return {cid: ts for cid, ts in self.dates.items() if cid in ids}


def test_stamp_targets_only_resume_verified_jobdiva_rows():
    service = _FakeDates({"111": _iso_days_ago(900)})
    cands = [
        dict(VERIFIED, candidate_id="111", source="JobDiva-TalentSearch"),
        # stated answer → freshness irrelevant
        dict(VERIFIED, candidate_id="222", source="JobDiva-TalentSearch",
             stated_current_employer="Acme"),
        # no employer signals → nothing to age
        {"candidate_id": "333", "source": "JobDiva-Applicants"},
        # external row → JobDiva resume dates don't apply
        dict(VERIFIED, candidate_id="AEMAAxyz", source="LinkedIn-Unipile"),
    ]
    asyncio.run(stamp_resume_freshness(cands, service=service))
    assert service.calls == [["111"]]
    assert cands[0]["resume_updated_at"] == service.dates["111"]
    assert "resume_updated_at" not in cands[1]
    assert "resume_updated_at" not in cands[2]
    assert "resume_updated_at" not in cands[3]
    assert employer_verification_state(cands[0]) == "verified_stale"


def test_stamp_skips_already_stamped_and_fails_open():
    pre_stamped = dict(VERIFIED, candidate_id="111", source="JobDiva-TalentSearch",
                       resume_updated_at=_iso_days_ago(10))
    service = _FakeDates({"111": _iso_days_ago(900)})
    asyncio.run(stamp_resume_freshness([pre_stamped], service=service))
    assert service.calls == []  # nothing left to fetch
    assert pre_stamped["resume_updated_at"] == pre_stamped["resume_updated_at"]

    class _Exploding:
        async def fetch_resume_dates(self, ids):
            raise RuntimeError("boom")

    cand = dict(VERIFIED, candidate_id="111", source="JobDiva-TalentSearch")
    asyncio.run(stamp_resume_freshness([cand], service=_Exploding()))
    assert "resume_updated_at" not in cand  # fail-open: unstamped, not raised


def test_stamp_disabled_by_knob(monkeypatch):
    from core import sourcing_config
    monkeypatch.setattr(sourcing_config, "EMPLOYER_STALE_RESUME_MONTHS", 0, raising=False)
    service = _FakeDates({"111": _iso_days_ago(900)})
    asyncio.run(stamp_resume_freshness(
        [dict(VERIFIED, candidate_id="111", source="JobDiva-TalentSearch")],
        service=service,
    ))
    assert service.calls == []


# ── webhook: stated answer persists + propagates person-wide ───────────────

def _run_webhook_with_transcriptions(transcriptions, client_name="Wells Fargo"):
    """Drive receive_interview_results with a mocked DB. fetchone returns the
    audit row first, then the monitored_jobs client row. Captures every
    UPDATE's jsonb blob and query text."""
    from routers.voice_agent import VoiceAgentInterviewWebhook, receive_interview_results

    mock_cur = MagicMock()
    mock_cur.fetchone.side_effect = [("cand-1", "job-1"), (client_name,)]
    state = {"blobs": [], "queries": []}

    def _execute(query, params=None):
        state["queries"].append(" ".join(query.split()))
        if params and "UPDATE sourced_candidates" in query:
            state["blobs"].append(json.loads(params[0]))
        mock_cur.rowcount = 1

    mock_cur.execute.side_effect = _execute
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch(
        "routers.voice_agent._check_and_fire_candidate_passed_notification",
        new_callable=AsyncMock,
    ), patch("routers.voice_agent.get_db_connection") as mock_db:
        mock_db.return_value.__enter__ = lambda s: mock_conn
        mock_db.return_value.__exit__ = MagicMock(return_value=False)
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="completed",
            candidate_score=80.0,
            total_score=100.0,
            hard_filter_status="passed",
            transcriptions=transcriptions,
        )
        asyncio.run(receive_interview_results(payload))
    return state


def test_webhook_persists_stated_answer_and_propagates():
    state = _run_webhook_with_transcriptions([
        {"question": EMPLOYER_QUESTION_TEXT, "answer": "I work at Cognizant"},
    ])
    main_blob = state["blobs"][0]
    assert main_blob["stated_current_employer"] == "I work at Cognizant"
    assert main_blob.get("stated_employer_at")
    assert "stated_employer_conflict" not in main_blob
    # person-wide propagation: a second UPDATE scoped by candidate only,
    # guarded on the value, carrying ONLY the stated fields
    prop_queries = [q for q in state["queries"] if "IS DISTINCT FROM" in q]
    assert len(prop_queries) == 1
    assert "jobdiva_id" not in prop_queries[0]
    prop_blob = state["blobs"][-1]
    assert set(prop_blob.keys()) == {"stated_current_employer", "stated_employer_at"}


def test_webhook_stamps_conflict_when_stated_employer_is_the_client():
    state = _run_webhook_with_transcriptions([
        {"question": EMPLOYER_QUESTION_TEXT, "answer": "currently at Wells Fargo, Charlotte"},
    ])
    main_blob = state["blobs"][0]
    assert "stated in interview" in main_blob["stated_employer_conflict"]
    # conflict text is job-specific and must NOT ride the propagation blob
    prop_blob = state["blobs"][-1]
    assert "stated_employer_conflict" not in prop_blob


def test_webhook_without_employer_answer_adds_nothing():
    state = _run_webhook_with_transcriptions([
        {"question": "Are you authorized to work in the US?", "answer": "Yes"},
    ])
    for blob in state["blobs"]:
        assert "stated_current_employer" not in blob
    assert not [q for q in state["queries"] if "IS DISTINCT FROM" in q]
