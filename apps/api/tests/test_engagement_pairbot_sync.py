import pytest

def test_candidate_id_to_email_mapping():
    """Test that candidate_id maps to the correct email even when filtered."""
    # Re-creating the pure logic block from _send_bulk_interview_core for unit testing
    payload_resumes = [
        {"email": "alice@test.com", "source_candidate_id": "1"},
        {"email": "bob@test.com", "source_candidate_id": "2"},
        {"email": "carol@test.com", "source_candidate_id": "3"},
    ]
    real_candidate_ids = ["1", "2", "3"]
    
    # Simulate Pairbot responding with only Bob (filtering Alice and Carol)
    response_data = {
        "interviews": [
            {"candidate_email": "bob@test.com", "interview_id": "int_bob"}
        ]
    }
    
    candidate_id_to_email = {
        str(real_candidate_ids[idx]): (r.get("email") or "").strip().lower()
        for idx, r in enumerate(payload_resumes)
    }
    
    data_list = response_data.get("interviews") or []
    interview_by_source_id = {str(item.get("source_candidate_id")): item for item in data_list if item.get("source_candidate_id")}
    interview_by_email = {str(item.get("candidate_email") or "").strip().lower(): item for item in data_list if item.get("candidate_email")}
    
    def get_interview_info(cand_id: str) -> dict:
        info = interview_by_source_id.get(str(cand_id))
        if not info:
            submitted_email = candidate_id_to_email.get(str(cand_id))
            info = interview_by_email.get(submitted_email) or {}
        return info

    assert get_interview_info("1") == {}
    assert get_interview_info("2") == {"candidate_email": "bob@test.com", "interview_id": "int_bob"}
    assert get_interview_info("3") == {}
