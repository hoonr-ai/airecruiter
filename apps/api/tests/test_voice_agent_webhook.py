from routers.voice_agent import TranscriptionItem, VoiceAgentInterviewWebhook


def test_webhook_accepts_null_hard_filter_candidate_scores():
    """PAI-154: null HF scores must not 422 the completed webhook."""
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
            TranscriptionItem(
                question="Describe a technical leadership example.",
                answer="I led a platform migration.",
                candidate_score=10.0,
                total_score=10.0,
                hard_filter_status="not_hard_filter",
                question_order=10,
            ),
        ],
    )
    assert payload.status == "completed"
    assert payload.transcriptions[0].candidate_score is None
    assert payload.transcriptions[1].candidate_score == 10.0
    assert payload.candidate_score == 100.0


def test_webhook_accepts_completed_payload_with_null_transcription_score():
    raw = {
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
    payload = (
        VoiceAgentInterviewWebhook.model_validate(raw)
        if hasattr(VoiceAgentInterviewWebhook, "model_validate")
        else VoiceAgentInterviewWebhook.parse_obj(raw)
    )
    assert payload.transcriptions[0].candidate_score is None
