from routers.candidates import _extract_rankings_hard_filter_details, _extract_needs_review_flags

def test_extract_needs_review_flags_tolerates_non_dict():
    # Helper should safely tolerate missing, malformed, or non-dict payloads
    for bad in (None, [], "string", 123):
        needs_review, questions = _extract_needs_review_flags(bad)
        assert needs_review is False
        assert questions == []

    # Should also tolerate payload["data"] being non-dict
    for bad_data in (None, [], "string", 123):
        needs_review, questions = _extract_needs_review_flags({"data": bad_data})
        assert needs_review is False
        assert questions == []

def test_extract_needs_review_flags_finds_nested_data():
    # Base level payload
    payload1 = {
        "hard_filter_needs_review": True,
        "needs_review_questions": ["q1"]
    }
    needs_review, questions = _extract_needs_review_flags(payload1)
    assert needs_review is True
    assert questions == ["q1"]

    # Nested under "data"
    payload2 = {
        "data": {
            "hard_filter_needs_review": True,
            "needs_review_questions": ["q2"]
        }
    }
    needs_review, questions = _extract_needs_review_flags(payload2)
    assert needs_review is True
    assert questions == ["q2"]

    # Missing flags completely
    payload3 = {"data": {"some_other_key": True}}
    needs_review, questions = _extract_needs_review_flags(payload3)
    assert needs_review is False
    assert questions == []

def test_extract_needs_review_flags_string_payload():
    import json
    # Valid JSON string
    payload_str = json.dumps({
        "hard_filter_needs_review": True,
        "needs_review_questions": ["q3"]
    })
    needs_review, questions = _extract_needs_review_flags(payload_str)
    assert needs_review is True
    assert questions == ["q3"]

    # Invalid JSON string
    needs_review, questions = _extract_needs_review_flags("invalid json")
    assert needs_review is False
    assert questions == []

    # None payload
    needs_review, questions = _extract_needs_review_flags(None)
    assert needs_review is False
    assert questions == []



def test_extract_rankings_hard_filter_details_from_webhook():
    # Test Priority 1: `hard_filter_results` in `engage_last_response`
    data_blob = {
        "engage_last_response": {
            "data": {
                "hard_filter_results": [
                    {
                        "question": "Are you authorized to work?",
                        "answer": "Yes I am.",
                        "hard_filter_status": "passed",
                        "reason": "Candidate said yes."
                    },
                    {
                        "question": "Will you need sponsorship?",
                        "answer": "No",
                        "pass_fail": "fail",
                        "reason": "Candidate said no, wait fail?"
                    },
                    {
                        "question": "Some non-hf question?",
                        "answer": "Idk",
                        "hard_filter_status": "not_hard_filter",
                        "reason": ""
                    }
                ]
            }
        }
    }

    result = _extract_rankings_hard_filter_details(data_blob, {}, {})
    
    assert len(result) == 2, "Should skip 'not_hard_filter' items"
    
    assert result[0]["question"] == "Are you authorized to work?"
    assert result[0]["answer"] == "Yes I am."
    assert result[0]["status"] == "Pass"
    assert result[0]["score"] is None
    assert result[0]["total_score"] is None
    
    assert result[1]["question"] == "Will you need sponsorship?"
    assert result[1]["answer"] == "No"
    assert result[1]["status"] == "Fail"

def test_extract_rankings_hard_filter_details_from_transcriptions():
    # Test Priority 2: fallback to transcriptions when hard_filter_results is absent
    data_blob = {}
    audit_response = {
        "transcriptions": [
            {
                "question": "What is your location?",
                "answer": "NY",
                "candidate_score": 10,
                "total_score": 10,
                "hard_filter_status": "passed",
                "reason": "In NY"
            },
            {
                "question": "How many years?",
                "answer": "5",
                "candidate_score": 0,
                "total_score": 10,
                "hard_filter_status": "failed",
                "reason": "Too few"
            }
        ]
    }

    result = _extract_rankings_hard_filter_details(data_blob, audit_response, {})
    
    assert len(result) == 2
    assert result[0]["status"] == "Pass"
    assert result[0]["score"] == 10
    assert result[0]["total_score"] == 10
    
    assert result[1]["status"] == "Fail"
    assert result[1]["score"] == 0
    assert result[1]["total_score"] == 10

def test_extract_rankings_hard_filter_details_tolerates_non_dict_data():
    # Failed-launch rows persist the raw PAIR envelope, whose "data" can be
    # null / list / str — the extractor must degrade to [] instead of raising.
    for bad in (None, [], "error", 0):
        data_blob = {"engage_last_response": {"data": bad}}
        assert _extract_rankings_hard_filter_details(data_blob, {}, {}) == []

    # Non-dict items inside hard_filter_results are skipped, not fatal.
    data_blob = {
        "engage_last_response": {
            "data": {
                "hard_filter_results": [
                    "garbage",
                    {"question": "Q", "answer": "A", "hard_filter_status": "passed"},
                ]
            }
        }
    }
    result = _extract_rankings_hard_filter_details(data_blob, {}, {})
    assert len(result) == 1
    assert result[0]["status"] == "Pass"

def test_extract_rankings_hard_filter_details_pending_status():
    data_blob = {
        "engage_last_response": {
            "data": {
                "hard_filter_results": [
                    {
                        "question": "Are you authorized?",
                        "answer": "Yes",
                        "hard_filter_status": "pending",
                    }
                ]
            }
        }
    }
    result = _extract_rankings_hard_filter_details(data_blob, {}, {})
    assert len(result) == 1
    assert result[0]["status"] == "Pending"


def test_fallback_to_webhook_transcriptions_when_audit_lacks_hf():
    """Real HF rows in webhook blob must win when audit only has ordinary questions."""
    audit_response = {
        "transcriptions": [
            {"question": "Describe leadership", "candidate_score": 8.0},
            {"question": "Why this role?", "candidate_score": 7.0},
        ]
    }
    data_blob = {
        "engage_last_response": {
            "data": {
                "transcriptions": [
                    {"question": "Are you authorized to work?", "hard_filter_status": "passed"},
                    {"question": "Need sponsorship?", "hard_filter_status": "failed"},
                ]
            }
        }
    }
    rows = _extract_rankings_hard_filter_details(data_blob, audit_response, {})
    assert len(rows) == 2
    assert rows[0]["status"] == "Pass"
    assert rows[1]["status"] == "Fail"
