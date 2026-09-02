"""Phase 2 of the employer-identification work (2026-09-02):

  - list-aware "current entry" semantics (an undated entry deep in the
    history is an unknown, not a current job);
  - end_client extraction support: a consultant currently placed AT the
    hiring client / a no-contact company is a conflict even though their
    employer of record is a vendor;
  - the post-interview backstop: the launch payload asks where the candidate
    works now, the webhook persists the answer as stated_current_employer,
    and the gates treat the candidate's own words as the strongest signal.
"""

from services.company_match import (
    _current_entries,
    collect_current_companies,
    collect_current_end_clients,
    collect_last_companies,
    client_conflict_reason,
    currently_employed_by_client,
    employed_by_client,
)
from services.no_contact import apply_no_contact_flag, check_no_contact
from services.employer_resolution import (
    employer_verification_state,
    has_confident_employer_signal,
)
from services.stated_employer import (
    EMPLOYER_QUESTION_TEXT,
    append_employer_question,
    extract_stated_employer,
    is_employer_question,
    stated_employer_conflict,
)
from routers.engagement import is_candidate_excluded_from_pair

KW = ["Kaiser", "Citibank", "Intuit"]


# ── list-aware currency ────────────────────────────────────────────────────

def test_undated_top_entry_is_current_but_undated_mid_entry_is_not():
    exp = [
        {"company": "Stripe"},              # undated, most recent slot → current
        {"company": "Old Shop"},            # undated, deeper → unknown, NOT current
        {"company": "Ancient Co", "end_date": "2015-01"},
    ]
    current = _current_entries(exp)
    assert [e["company"] for e in current] == ["Stripe"]
    assert collect_current_companies({"company_experience": exp}) == ["Stripe"]
    # The undated mid entry now counts as PAST (pre-phase-2 it was "current").
    # Among past entries the existing dated-beats-undated rule still applies,
    # so the parseable 2015 end date wins the "last employer" slot.
    assert collect_last_companies({"company_experience": exp}) == ["Ancient Co"]


def test_explicit_markers_beat_position():
    exp = [
        {"company": "A", "current": False},                  # explicit False, even in top slot
        {"company": "B", "end_date": "Present"},             # explicit present marker
        {"company": "C", "is_current": True},                # explicit True deep in list
    ]
    assert [e["company"] for e in _current_entries(exp)] == ["B", "C"]


def test_undated_history_no_longer_needs_explicit_markers():
    # Pre-phase-2, {"company": "Citibank"} with no end date read as CURRENT
    # anywhere in the list (see the workaround in test_no_contact.py).
    cand = {
        "company_experience": [
            {"company": "Stripe", "is_current": True},
            {"company": "Citibank"},
            {"company": "Kaiser"},
        ]
    }
    assert collect_current_companies(cand) == ["Stripe"]
    assert collect_last_companies(cand) == ["Citibank"]


# ── end-client (placement) matching ────────────────────────────────────────

PLACED_AT_WALMART = {
    "company_experience": [
        {"company": "Tata Consultancy Services", "end_date": "Present", "end_client": "Walmart"},
        {"company": "Infosys", "end_date": "2021-05", "end_client": "Target"},
    ]
}


def test_collect_current_end_clients_is_current_only():
    assert collect_current_end_clients(PLACED_AT_WALMART) == ["Walmart"]


def test_placement_at_client_is_a_conflict_despite_vendor_employer():
    hit = employed_by_client(PLACED_AT_WALMART, "Walmart Inc")
    assert hit == {"company": "Walmart", "relation": "placement"}
    reason = client_conflict_reason(hit, "Walmart Inc")
    assert "placed at the hiring client" in reason.lower()
    # search-time hard drop for external sources honors placements too
    assert currently_employed_by_client(PLACED_AT_WALMART, "Walmart Inc") == "Walmart"


def test_past_placement_is_not_a_conflict():
    assert employed_by_client(PLACED_AT_WALMART, "Target") is None


def test_gate_reason_for_placement():
    excluded, reason = is_candidate_excluded_from_pair(dict(PLACED_AT_WALMART), "Walmart")
    assert excluded
    assert reason == "Placed at Hiring Client (current engagement)"


def test_no_contact_flags_current_placement():
    cand = {
        "company_experience": [
            {"company": "Wipro", "end_date": "Present", "end_client": "Kaiser Permanente"},
        ]
    }
    hit = check_no_contact(cand, KW)
    assert hit == {"company": "Kaiser Permanente", "keyword": "Kaiser", "relation": "placement"}
    assert apply_no_contact_flag(cand) is True
    assert "placement" in cand["no_contact_reason"].lower()


# ── the interview question ─────────────────────────────────────────────────

def test_append_employer_question_appends_once_in_pair_schema():
    qs = append_employer_question([
        {"question_text": "Are you authorized to work in the US?", "pass_criteria": "",
         "is_default": True, "category": "default", "is_hard_filter": True},
    ])
    assert qs[-1]["question_text"] == EMPLOYER_QUESTION_TEXT
    assert qs[-1]["category"] == "logistics"
    assert qs[-1]["is_hard_filter"] is False
    # idempotent: a second append (or a reworded variant with the stem) dedups
    assert len(append_employer_question(qs)) == len(qs)
    reworded = [{"question_text": "Tell me, which company do you currently work for today?"}]
    assert len(append_employer_question(reworded)) == 1


def test_append_employer_question_respects_kill_switch(monkeypatch):
    from core import sourcing_config
    monkeypatch.setattr(sourcing_config, "EMPLOYER_QUESTION_ENABLED", False, raising=False)
    assert append_employer_question([]) == []


def test_is_employer_question_matches_humanized_variants():
    assert is_employer_question("Which company do you currently work for?")
    assert is_employer_question("  which Company do you CURRENTLY work for, please?")
    assert not is_employer_question("Which company did you work for in 2019?")


# ── answer extraction from webhook transcriptions ──────────────────────────

def test_extract_stated_employer_finds_the_answer_row():
    transcriptions = [
        {"question": "Are you authorized to work in the US?", "answer": "Yes"},
        {"question": EMPLOYER_QUESTION_TEXT, "answer": "  I work at   Cognizant right now "},
        {"question": "What is your notice period?", "answer": "2 weeks"},
    ]
    assert extract_stated_employer(transcriptions) == "I work at Cognizant right now"


def test_extract_stated_employer_skips_non_answers():
    assert extract_stated_employer([
        {"question": EMPLOYER_QUESTION_TEXT, "answer": "n/a"},
    ]) is None
    assert extract_stated_employer([
        {"question": EMPLOYER_QUESTION_TEXT, "answer": ""},
    ]) is None
    assert extract_stated_employer(None) is None
    assert extract_stated_employer("garbage") is None


def test_extract_stated_employer_caps_length():
    long_answer = "word " * 200
    got = extract_stated_employer([{"question": EMPLOYER_QUESTION_TEXT, "answer": long_answer}])
    assert got is not None and len(got) <= 300


# ── conflict detection on the stated answer ────────────────────────────────

def test_stated_conflict_no_contact_and_client():
    assert "No-Contact Company (Kaiser)" in stated_employer_conflict(
        "I'm a nurse at Kaiser Permanente in Oakland", ""
    )
    assert "Employed by Hiring Client (Wells Fargo)" in stated_employer_conflict(
        "currently with wells fargo, charlotte office", "Wells Fargo"
    )
    assert stated_employer_conflict("I am between jobs right now", "Wells Fargo") is None
    assert stated_employer_conflict("", "Wells Fargo") is None


# ── stated answer drives the gates and the flags ───────────────────────────

def test_gate_stated_answer_beats_structured_signals():
    # Extraction says Stripe; the candidate SAID they work at the client.
    cand = {
        "candidate_id": "1",
        "company_experience": [{"company": "Stripe", "end_date": "Present"}],
        "stated_current_employer": "I actually joined Wells Fargo last month",
    }
    excluded, reason = is_candidate_excluded_from_pair(cand, "Wells Fargo")
    assert excluded
    assert reason == "Employed by Hiring Client (stated in interview)"


def test_gate_stated_answer_counts_as_judged_not_blind():
    cand = {"candidate_id": "1", "stated_current_employer": "self-employed consultant"}
    excluded, reason = is_candidate_excluded_from_pair(cand, "Wells Fargo")
    assert not excluded and reason == ""


def test_no_contact_flag_from_stated_answer():
    cand = {"candidate_id": "1", "data": {"stated_current_employer": "I work for Citi Bank"}}
    hit = check_no_contact(cand, KW)
    assert hit is not None and hit["relation"] == "stated" and hit["keyword"] == "Citibank"


def test_stated_answer_is_confident_and_verified():
    cand = {"data": {"stated_current_employer": "Cognizant"}}
    assert has_confident_employer_signal(cand)
    assert employer_verification_state(cand) == "verified"
