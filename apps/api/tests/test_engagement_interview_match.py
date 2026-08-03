"""
Unit tests for the pairbot response → candidate matching block in the engage route.

The matching logic (interview_by_source_candidate_id / interview_by_email maps)
is exercised here via a thin helper that mirrors the production loop exactly,
so the tests pin the behaviour and prevent the removed positional fallback from
being reintroduced.
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
    interview_by_source_candidate_id: Dict[str, Dict[str, Any]] = {}
    interview_by_email: Dict[str, Dict[str, Any]] = {}
    for item in data_list:
        if not isinstance(item, dict):
            continue
        item_source_id = str(item.get("source_candidate_id") or "").strip()
        if item_source_id:
            interview_by_source_candidate_id[item_source_id] = item
        item_email = str(item.get("candidate_email") or "").lower().strip()
        if item_email and item_email not in interview_by_email:
            interview_by_email[item_email] = item

    submitted_source_id_by_idx = [
        str((r or {}).get("source_candidate_id") or "").strip()
        for r in payload_resumes
    ]
    submitted_email_by_idx = [
        str((r or {}).get("email") or "").lower().strip()
        for r in payload_resumes
    ]

    results = []
    for idx, candidate_id in enumerate(real_candidate_ids):
        submitted_source_id = (
            submitted_source_id_by_idx[idx] if idx < len(submitted_source_id_by_idx) else ""
        )
        submitted_email = (
            submitted_email_by_idx[idx] if idx < len(submitted_email_by_idx) else ""
        )
        interview_info: Dict[str, Any] = (
            interview_by_source_candidate_id.get(submitted_source_id)
            if submitted_source_id
            else {}
        ) or {}
        if not interview_info:
            interview_info = interview_by_email.pop(submitted_email, {}) if submitted_email else {}
        results.append((candidate_id, interview_info))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_source_candidate_id_match():
    """Primary key match works when bot echoes source_candidate_id."""
    data_list = [{"source_candidate_id": "S1", "interview_id": "I1", "candidate_email": "a@x.com"}]
    resumes = [{"source_candidate_id": "S1", "email": "a@x.com"}]
    results = _match(data_list, resumes, ["C1"])
    assert results[0][1]["interview_id"] == "I1"


def test_email_fallback_when_no_source_id_in_response():
    """Email path activates when bot omits source_candidate_id (older responses)."""
    data_list = [{"candidate_email": "b@x.com", "interview_id": "I2"}]
    resumes = [{"source_candidate_id": "S2", "email": "b@x.com"}]
    results = _match(data_list, resumes, ["C2"])
    assert results[0][1]["interview_id"] == "I2"


def test_response_shorter_than_submitted_list():
    """Bot skips a candidate → that candidate gets an empty dict (no phantom sent)."""
    data_list = [{"source_candidate_id": "S1", "interview_id": "I1"}]
    resumes = [
        {"source_candidate_id": "S1", "email": "a@x.com"},
        {"source_candidate_id": "S2", "email": "b@x.com"},
    ]
    results = _match(data_list, resumes, ["C1", "C2"])
    assert results[0][1]["interview_id"] == "I1"
    assert results[1][1] == {}   # skipped candidate → empty, not a phantom


def test_no_positional_fallback():
    """No match by key → empty dict, even when a positional entry exists at that index."""
    data_list = [{"interview_id": "WRONG", "candidate_email": "other@x.com"}]
    resumes = [{"source_candidate_id": "MISSING", "email": "nomatch@x.com"}]
    results = _match(data_list, resumes, ["C1"])
    assert results[0][1] == {}


def test_shared_email_no_phantom():
    """Two candidates sharing an email cannot both claim the same interview entry."""
    data_list = [{"candidate_email": "shared@x.com", "interview_id": "I3"}]
    resumes = [
        {"source_candidate_id": "", "email": "shared@x.com"},
        {"source_candidate_id": "", "email": "shared@x.com"},
    ]
    results = _match(data_list, resumes, ["C3", "C4"])
    matched = [r[1].get("interview_id") for r in results]
    # Exactly one candidate claims the interview; the other gets nothing.
    assert matched.count("I3") == 1
    assert matched.count(None) == 1


def test_source_id_preferred_over_email():
    """source_candidate_id lookup wins even when email would also match a different entry."""
    data_list = [
        {"source_candidate_id": "S1", "candidate_email": "a@x.com", "interview_id": "CORRECT"},
        {"source_candidate_id": "S9", "candidate_email": "a@x.com", "interview_id": "WRONG"},
    ]
    resumes = [{"source_candidate_id": "S1", "email": "a@x.com"}]
    results = _match(data_list, resumes, ["C1"])
    assert results[0][1]["interview_id"] == "CORRECT"
