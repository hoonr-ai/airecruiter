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
        if params and "UPDATE sourced_candidates" in query:
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
    assert "first_attempted_at" not in blob
    assert "first_completed_at" not in blob
    assert any("jsonb_set(doc, '{first_attempted_at}'" in q for q in state["execute_queries"])
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


# ---------------------------------------------------------------------------
# Tests for closing-bot-sentence exclusion (PR #517)
# ---------------------------------------------------------------------------

def _is_closing_sentence(item: dict) -> bool:
    """
    Mirror of the closing-sentence exclusion logic in engagement.py so we can
    unit-test it without standing up a DB. Keep in sync with that function.

    Definitive signal: total_score == 0.0 exactly (not None — see intentional
    tradeoff comment in engagement.py). No real evaluation question is ever
    scored out of 0.
    """
    score = item.get("candidate_score")
    total = item.get("total_score", 10.0)
    hf = str(item.get("hard_filter_status") or "").strip().lower().replace(" ", "_")
    is_hard_filter = hf not in ("", "not_hard_filter", "na", "n/a", "none")

    try:
        score_value = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_value = None

    try:
        total_value = float(total) if total is not None else None
    except (TypeError, ValueError):
        total_value = None

    return (
        score_value == 0.0
        and total_value == 0.0       # exact 0 only — None is intentionally excluded
        and not is_hard_filter
    )


def test_closing_sentence_is_excluded():
    """Bot closing sentence (score=0, total=0, no answer, any order) is filtered out."""
    item = {
        "question": "Thank you for your time! A recruiter will be in touch.",
        "answer": None,
        "candidate_score": 0.0,
        "total_score": 0.0,
        "hard_filter_status": None,
        "question_order": 13,  # Partner API might send a real order
    }
    assert _is_closing_sentence(item) is True


def test_closing_sentence_with_real_answer_text_is_excluded():
    """Regression: newer pairbot populates the closing-sentence answer with the full
    thank-you paragraph. The old logic (checking for empty/— answer) missed this.
    The definitive signal is total_score == 0, regardless of answer text."""
    item = {
        "question": "Thank you, Dmitry, for sharing your background and insights today.",
        "answer": (
            "Thank you, Dmitry, for sharing your background and insights today. "
            "I appreciated hearing about your hands-on experience with Tableau for "
            "data visualization. A human recruiter will be in touch soon to discuss "
            "the next steps and address any questions you might have. Have a wonderful day!"
        ),
        "candidate_score": 0.0,
        "total_score": 0,   # Comes in as integer 0 from pairbot
        "hard_filter_status": None,
        "question_order": 5,  # Pairbot may assign any order
    }
    assert _is_closing_sentence(item) is True


def test_closing_sentence_with_null_total_score_is_not_excluded():
    """Intentional tradeoff: explicit null total_score is NOT treated as a closing
    sentence. A real scored question (Q10+) where the partner API transiently omits
    total_score should surface in the email rather than be silently dropped.
    If pairbot sends null for the actual closing sentence, that item passes through —
    the safer choice over accidental data loss. See engagement.py for full rationale."""
    item = {
        "question": "Thank you for sharing!",
        "answer": None,
        "candidate_score": 0.0,
        "total_score": None,   # partner API sends "total_score": null
        "hard_filter_status": None,
        "question_order": 0,
    }
    result = _is_closing_sentence(item)
    # total_value is None → does NOT match total_value == 0.0 → NOT excluded.
    assert result is False


def test_info_only_question_at_score_boundary_is_not_excluded():
    """Guard: an info-only question (Q2-9) with candidate_score==0.0 must NOT be
    filtered out as a closing sentence, provided its total_score is non-zero.
    Info-only questions always carry total_score > 0 in production (pairbot contract).
    This test makes that assumption explicit."""
    item = {
        "question": "What is your current notice period?",
        "answer": "Two weeks.",
        "candidate_score": 0.0,   # Low/zero score is still a real answer
        "total_score": 10.0,      # Info-only questions are always scored out of 10
        "hard_filter_status": None,
        "question_order": 5,      # Q5 → info-only range
    }
    assert _is_closing_sentence(item) is False


def test_real_skipped_scored_question_is_not_excluded():
    """A genuinely scored question (Q10+) that the candidate skipped must NOT be filtered.
    total_score is 10.0 (not 0.0) because real scored questions are always out of 10 —
    that is exactly what separates them from the bot closing sentence (total_score==0).
    A Q10+ with total_score==0 would be misclassified; that is a pairbot contract
    violation, not something we defend against here."""
    item = {
        "question": "Describe a time you led a cross-functional team.",
        "answer": None,
        "candidate_score": 0.0,
        "total_score": 10.0,    # Real scored questions are out of 10, never 0
        "hard_filter_status": None,
        "question_order": 10,   # Q10+ → is_scored_question = True
    }
    assert _is_closing_sentence(item) is False
