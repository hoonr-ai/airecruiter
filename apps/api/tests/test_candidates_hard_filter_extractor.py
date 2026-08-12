from routers.candidates import _extract_rankings_hard_filter_details

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
