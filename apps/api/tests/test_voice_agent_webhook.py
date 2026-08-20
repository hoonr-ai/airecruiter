import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from routers.candidates import _extract_rankings_hard_filter_details
from routers.hard_filter_utils import count_pending_hard_filters
from routers.voice_agent import (
    TranscriptionItem,
    VoiceAgentInterviewWebhook,
    effective_status_for_webhook,
    receive_interview_results,
    should_persist_engage_scores,
)


def _validate_webhook(raw: dict) -> VoiceAgentInterviewWebhook:
    return VoiceAgentInterviewWebhook.model_validate(raw)


def test_transcription_accepts_null_candidate_score():
    """PAI-154: null HF transcription scores must not 422 the completed webhook."""
    payload = VoiceAgentInterviewWebhook(
        interview_id="12345",
        status="completed",
        jobdiva_id="26-25391",
        hard_filter_status="passed",
        total_score=120.0,
        candidate_score=100.0,
        transcriptions=[
            TranscriptionItem(
                question="Are you open to exploring new job opportunities?",
                answer="Yes",
                candidate_score=None,
                total_score=10.0,
                hard_filter_status="pass",
                question_order=1,
            ),
        ],
    )
    assert payload.transcriptions[0].candidate_score is None


def test_webhook_accepts_null_transcription_score_from_raw_dict():
    """Raw JSON with null per-row score must validate."""
    payload = _validate_webhook(
        {
            "interview_id": "99",
            "status": "completed",
            "hard_filter_status": "passed",
            "candidate_score": 80.0,
            "total_score": 100.0,
            "transcriptions": [
                {
                    "question": "Are you a US citizen?",
                    "answer": "Yes",
                    "candidate_score": None,
                    "hard_filter_status": "passed",
                }
            ],
        }
    )
    assert payload.transcriptions[0].candidate_score is None


def test_webhook_accepts_null_question_and_total_score():
    payload = _validate_webhook(
        {
            "interview_id": "100",
            "status": "completed",
            "hard_filter_status": "passed",
            "candidate_score": 80.0,
            "transcriptions": [
                {
                    "question": None,
                    "answer": "Yes",
                    "candidate_score": None,
                    "total_score": None,
                    "hard_filter_status": "pending",
                }
            ],
        }
    )
    assert payload.transcriptions[0].question is None
    assert payload.transcriptions[0].total_score is None


def test_pending_hard_filter_rows_surface_in_extractor():
    """Explicit pending tokens render as Pending; unmarked rows are not hard filters."""
    data_blob = {
        "engage_last_response": {
            "data": {
                "hard_filter_results": [
                    {
                        "question": "Are you authorized to work?",
                        "answer": "Yes",
                        "hard_filter_status": "pending",
                    },
                    {
                        "question": "Describe a leadership example",
                        "answer": "Led a team of five",
                        "hard_filter_status": "not_hard_filter",
                    },
                    {
                        "question": "Why this role?",
                        "answer": "Growth",
                    },
                ]
            }
        }
    }
    rows = _extract_rankings_hard_filter_details(data_blob, {}, {})
    assert len(rows) == 1
    assert rows[0]["status"] == "Pending"


def test_effective_status_mapping_and_score_persist_gate():
    assert effective_status_for_webhook("completed", "passed") == "passed"
    assert effective_status_for_webhook("completed", "failed") == "failed"
    assert effective_status_for_webhook("completed", "pending") == "in_progress"
    assert effective_status_for_webhook("completed", "passed", has_pending_hf=True) == "in_progress"
    assert effective_status_for_webhook("in_progress", "passed") == "in_progress"
    assert effective_status_for_webhook("failed", None) == "failed"
    assert effective_status_for_webhook("Completed", "passed") == "passed"
    assert effective_status_for_webhook("COMPLETED", "failed") == "failed"
    assert should_persist_engage_scores("completed", "passed") is True
    assert should_persist_engage_scores("Completed", "passed") is True
    assert should_persist_engage_scores("completed", "in_progress") is False
    assert should_persist_engage_scores("failed", "failed") is False
    assert should_persist_engage_scores("in_progress", "in_progress") is False


@contextmanager
def _mock_webhook_db(audit_row=("cand-1", "job-1"), primary_rowcount=1, fallback_rowcount=1):
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = audit_row
    state = {"blobs": [], "commits": 0, "execute_queries": []}

    def _execute(query, params=None):
        state["execute_queries"].append(query)
        if params and "sourced_candidates" in query:
            state["blobs"].append(json.loads(params[0]))
        if "jobdiva_id = %s OR jobdiva_id = %s" in query:
            mock_cur.rowcount = primary_rowcount
        elif "WHERE candidate_id = %s" in query and "jobdiva_id" not in query:
            mock_cur.rowcount = fallback_rowcount

    mock_cur.execute.side_effect = _execute
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.commit.side_effect = lambda: state.__setitem__("commits", state["commits"] + 1)

    notify_patch = patch(
        "routers.voice_agent._check_and_fire_candidate_passed_notification",
        new_callable=AsyncMock,
    )
    db_patch = patch("routers.voice_agent.get_db_connection")

    with notify_patch as mock_notify, db_patch as mock_db:
        mock_db.return_value.__enter__ = lambda s: mock_conn
        mock_db.return_value.__exit__ = MagicMock(return_value=False)
        yield state
        state["notify"] = mock_notify


def test_receive_interview_results_writes_scores_on_completed():
    with _mock_webhook_db() as state:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="completed",
            candidate_score=80.0,
            total_score=100.0,
            hard_filter_status="passed",
        )
        asyncio.run(receive_interview_results(payload))

    blob = state["blobs"][-1]
    assert blob["engage_status"] == "passed"
    assert blob["engage_score"] == 80.0
    assert blob["engage_total_score"] == 100.0
    assert state["commits"] == 1
    state["notify"].assert_called_once()


def test_receive_interview_results_skips_scores_on_in_progress():
    with _mock_webhook_db() as state:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="in_progress",
            candidate_score=40.0,
            total_score=100.0,
        )
        asyncio.run(receive_interview_results(payload))

    blob = state["blobs"][-1]
    assert blob["engage_status"] == "in_progress"
    assert "engage_score" not in blob
    state["notify"].assert_not_called()


def test_receive_interview_results_titlecase_completed_maps_and_writes_scores():
    with _mock_webhook_db() as state:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="Completed",
            candidate_score=90.0,
            total_score=100.0,
            hard_filter_status="passed",
        )
        asyncio.run(receive_interview_results(payload))

    blob = state["blobs"][-1]
    assert blob["engage_status"] == "passed"
    assert blob["engage_score"] == 90.0


def test_pending_hf_webhook_persists_in_progress_without_scores():
    with _mock_webhook_db() as state:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="completed",
            candidate_score=80.0,
            total_score=100.0,
            hard_filter_status="passed",
            transcriptions=[
                TranscriptionItem(
                    question="Are you authorized?",
                    answer="Yes",
                    hard_filter_status="pending",
                )
            ],
        )
        asyncio.run(receive_interview_results(payload))

    blob = state["blobs"][-1]
    assert blob["engage_status"] == "in_progress"
    assert "engage_score" not in blob
    assert blob["engage_hard_filter_pending_count"] == 1
    state["notify"].assert_not_called()


def test_receive_interview_results_fallback_update_when_job_mapping_missing():
    with _mock_webhook_db(primary_rowcount=0, fallback_rowcount=1) as state:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="completed",
            candidate_score=70.0,
            total_score=100.0,
            hard_filter_status="passed",
        )
        asyncio.run(receive_interview_results(payload))

    assert len(state["blobs"]) == 2
    assert state["blobs"][-1]["engage_status"] == "passed"
    assert any("WHERE candidate_id = %s" in q and "jobdiva_id" not in q for q in state["execute_queries"])


def test_receive_interview_results_without_audit_row_uses_payload_ids():
    with _mock_webhook_db(audit_row=None) as state:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="completed",
            jobdiva_id="26-99999",
            candidate_id="cand-from-payload",
            candidate_score=75.0,
            total_score=100.0,
            hard_filter_status="passed",
        )
        asyncio.run(receive_interview_results(payload))

    blob = state["blobs"][-1]
    assert blob["engage_status"] == "passed"
    assert state["commits"] == 1


def test_count_pending_hard_filters_dedupes_and_skips_ordinary_rows():
    payload = VoiceAgentInterviewWebhook(
        interview_id="1",
        status="completed",
        hard_filter_results=[
            {"question": "Authorized?", "hard_filter_status": "pending"},
        ],
        transcriptions=[
            {"question": "Authorized?", "hard_filter_status": "pending"},
            {"question": "Leadership example", "candidate_score": 8.0},
            {"question": "Normal q", "hard_filter_status": "not_hard_filter"},
        ],
    )
    assert count_pending_hard_filters(payload.hard_filter_results, payload.transcriptions) == 1
