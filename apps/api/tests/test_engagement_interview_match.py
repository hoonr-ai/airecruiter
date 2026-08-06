"""
Unit tests for the pairbot response → candidate matching block in the engage route.

Pairbot does not echo source_candidate_id in data[]; all matching is by email.
The helper below mirrors the production loop exactly so tests pin the behaviour
and prevent the removed positional fallback from being reintroduced.
"""
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Mirror of the production matching block, kept as a pure function so tests
# don't need to stand up the full FastAPI / DB stack.
# ---------------------------------------------------------------------------

def _match(
    data_list: List[Dict[str, Any]],
    payload_resumes: List[Dict[str, Any]],
    real_candidate_ids: List[str],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return [(candidate_id, interview_info), ...] in real_candidate_ids order."""
    interview_by_email: Dict[str, Dict[str, Any]] = {}
    for item in data_list:
        if not isinstance(item, dict):
            continue
        item_email = str(item.get("candidate_email") or "").lower().strip()
        if item_email and item_email not in interview_by_email:
            interview_by_email[item_email] = item

    submitted_email_by_idx = [
        str((r or {}).get("email") or "").lower().strip()
        for r in payload_resumes
    ]

    results = []
    for idx, candidate_id in enumerate(real_candidate_ids):
        submitted_email = (
            submitted_email_by_idx[idx] if idx < len(submitted_email_by_idx) else ""
        )
        interview_info = interview_by_email.pop(submitted_email, {}) if submitted_email else {}
        results.append((candidate_id, interview_info))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_email_match():
    """Standard case: bot echoes candidate_email and match succeeds."""
    data_list = [{"candidate_email": "a@x.com", "interview_id": "I1"}]
    resumes = [{"source_candidate_id": "S1", "email": "a@x.com"}]
    results = _match(data_list, resumes, ["C1"])
    assert results[0][1]["interview_id"] == "I1"


def test_response_shorter_than_submitted_list():
    """Bot skips a candidate → that candidate gets an empty dict (no phantom sent)."""
    data_list = [{"candidate_email": "a@x.com", "interview_id": "I1"}]
    resumes = [
        {"source_candidate_id": "S1", "email": "a@x.com"},
        {"source_candidate_id": "S2", "email": "b@x.com"},
    ]
    results = _match(data_list, resumes, ["C1", "C2"])
    assert results[0][1]["interview_id"] == "I1"
    assert results[1][1] == {}  # skipped candidate → empty, not a phantom


def test_no_positional_fallback():
    """No email match → empty dict, even when a positional entry exists at that index."""
    data_list = [{"candidate_email": "other@x.com", "interview_id": "WRONG"}]
    resumes = [{"source_candidate_id": "S1", "email": "nomatch@x.com"}]
    results = _match(data_list, resumes, ["C1"])
    assert results[0][1] == {}


def test_no_email_candidate_gets_failed():
    """Candidate with no email cannot match any response entry."""
    data_list = [{"candidate_email": "a@x.com", "interview_id": "I1"}]
    resumes = [{"source_candidate_id": "S1", "email": ""}]
    results = _match(data_list, resumes, ["C1"])
    assert results[0][1] == {}


def test_shared_email_no_phantom():
    """Two candidates sharing an email cannot both claim the same interview entry."""
    data_list = [{"candidate_email": "shared@x.com", "interview_id": "I3"}]
    resumes = [
        {"source_candidate_id": "S1", "email": "shared@x.com"},
        {"source_candidate_id": "S2", "email": "shared@x.com"},
    ]
    results = _match(data_list, resumes, ["C3", "C4"])
    matched = [r[1].get("interview_id") for r in results]
    # Exactly one candidate claims the interview; the other gets nothing.
    assert matched.count("I3") == 1
    assert matched.count(None) == 1


def test_duplicate_email_in_response_first_wins():
    """If data[] has two entries for the same email, the first is used."""
    data_list = [
        {"candidate_email": "a@x.com", "interview_id": "FIRST"},
        {"candidate_email": "a@x.com", "interview_id": "SECOND"},
    ]
    resumes = [{"source_candidate_id": "S1", "email": "a@x.com"}]
    results = _match(data_list, resumes, ["C1"])
    assert results[0][1]["interview_id"] == "FIRST"
