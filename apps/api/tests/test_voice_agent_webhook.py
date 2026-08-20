import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from routers.candidates import _extract_rankings_hard_filter_details
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
    """Pending/null HF rows must render as Pending, not be dropped."""
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
                        "question": "Sponsorship?",
                        "answer": "No",
                        "hard_filter_status": None,
                    },
                ]
            }
        }
    }
    rows = _extract_rankings_hard_filter_details(data_blob, {}, {})
    assert len(rows) == 2
    assert rows[0]["status"] == "Pending"
    assert rows[1]["status"] == "Pending"


def test_effective_status_mapping_and_score_persist_gate():
    assert effective_status_for_webhook("completed", "passed") == "passed"
    assert effective_status_for_webhook("completed", "failed") == "failed"
    assert effective_status_for_webhook("in_progress", "passed") == "in_progress"
    assert effective_status_for_webhook("failed", None) == "failed"
    assert effective_status_for_webhook("Completed", "passed") == "passed"
    assert effective_status_for_webhook("COMPLETED", "failed") == "failed"
    assert should_persist_engage_scores("completed") is True
    assert should_persist_engage_scores("Completed") is True
    assert should_persist_engage_scores("failed") is False
    assert should_persist_engage_scores("in_progress") is False


@contextmanager
def _mock_webhook_db(audit_row=("cand-1", "job-1"), rowcount=1):
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = audit_row
    mock_cur.rowcount = rowcount
    blobs = []

    def _execute(query, params=None):
        if params and "sourced_candidates" in query:
            blobs.append(json.loads(params[0]))

    mock_cur.execute.side_effect = _execute
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch("routers.voice_agent.get_db_connection") as mock_db:
        mock_db.return_value.__enter__ = lambda s: mock_conn
        mock_db.return_value.__exit__ = MagicMock(return_value=False)
        yield blobs


def test_receive_interview_results_writes_scores_on_completed():
    with _mock_webhook_db() as blobs:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="completed",
            candidate_score=80.0,
            total_score=100.0,
            hard_filter_status="passed",
        )
        asyncio.run(receive_interview_results(payload))

    assert blobs[0]["engage_status"] == "passed"
    assert blobs[0]["engage_score"] == 80.0
    assert blobs[0]["engage_total_score"] == 100.0


def test_receive_interview_results_skips_scores_on_in_progress():
    with _mock_webhook_db() as blobs:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="in_progress",
            candidate_score=40.0,
            total_score=100.0,
        )
        asyncio.run(receive_interview_results(payload))

    assert blobs[0]["engage_status"] == "in_progress"
    assert "engage_score" not in blobs[0]


def test_receive_interview_results_titlecase_completed_maps_and_writes_scores():
    with _mock_webhook_db() as blobs:
        payload = VoiceAgentInterviewWebhook(
            interview_id="42",
            status="Completed",
            candidate_score=90.0,
            total_score=100.0,
            hard_filter_status="passed",
        )
        asyncio.run(receive_interview_results(payload))

    assert blobs[0]["engage_status"] == "passed"
    assert blobs[0]["engage_score"] == 90.0
