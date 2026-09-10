"""Unit tests for services/screen_facts.py — the reusable-fact spec table.

Two properties carry the weight here:

1. **Round trip.** Every `recheck_question` this module emits must classify back
   to its own `fact_key`. If it doesn't, a re-asked fact is captured under no key
   at all, so it stays "stale" forever and every future screen re-asks it.

2. **Phrasing coverage.** The same fact is worded three ways depending on
   screening level — the stored L1/L1.5 text, the L0.5 boolean rewrite from
   `engagement._to_boolean_question_text`, and the re-ask text here. The original
   inline if/elif chain in candidates.py only matched the first, which meant an
   L0.5 job produced zero reusable facts: exactly the cohort reuse targets.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY", "DATABASE_URL",
):
    os.environ.setdefault(_k, "test")

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from services import screen_facts as sf  # noqa: E402

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _stored(fact_key, *, age_days=0, ttl_days=None):
    spec = sf.spec_for(fact_key)
    ttl = ttl_days if ttl_days is not None else spec.ttl_days
    captured = NOW - timedelta(days=age_days)
    return sf.StoredFact(
        fact_key=fact_key,
        fact_value="Yes",
        normalized={"bool": True},
        captured_at=captured,
        expires_at=captured + timedelta(days=ttl),
        jobdiva_id="26-00001",
    )


# ── round trip ───────────────────────────────────────────────────────────

def test_every_recheck_question_classifies_back_to_its_own_fact():
    for spec in sf.FACT_SPECS:
        assert spec.recheck_question, f"{spec.fact_key} has no recheck_question"
        assert sf.classify_question(spec.recheck_question) == spec.fact_key, (
            f"{spec.fact_key}: re-ask text does not classify back to itself"
        )


def test_every_spec_has_a_ttl_and_a_distinct_eval_alias():
    aliases = [s.eval_alias for s in sf.FACT_SPECS]
    assert len(aliases) == len(set(aliases))
    for spec in sf.FACT_SPECS:
        assert spec.ttl_days > 0


# ── phrasing coverage ────────────────────────────────────────────────────

def test_l05_boolean_rewrites_are_recognized():
    # Exactly the strings engagement._to_boolean_question_text produces.
    cases = {
        "Do you have recent experience in a role similar to this position?": "current_role",
        "Are you currently based in the required job region for this role?": "current_location",
        "Are you available to start within the required timeline for this role?": "availability",
        "Are your compensation expectations aligned with this role?": "expected_pay_rate",
        "Are you eligible to work in at least one of these arrangements: W2, "
        "Subcontractor, or Independent Contractor?": "work_arrangements",
        "Are you authorized to work indefinitely for any employer in the United "
        "States?": "work_authorization",
        "Will you now or in the future require visa sponsorship to continue working "
        "in the United States?": "visa_sponsorship",
    }
    for question, expected in cases.items():
        assert sf.classify_question(question) == expected, question


def test_stored_l1_phrasings_are_recognized():
    cases = {
        "What is your current location?": "current_location",
        "Tell me about your current or most recent role.": "current_role",
        "What working arrangements are you open to?": "work_arrangements",
        "What is your availability to start?": "availability",
        "What are your compensation expectations?": "expected_pay_rate",
        "Are you open to exploring new job opportunities?": "actively_looking",
    }
    for question, expected in cases.items():
        assert sf.classify_question(question) == expected, question


def test_work_authorization_requires_both_country_and_authorization():
    # "authorized to work in Canada" is a different fact; reusing it as US
    # authorization would be a real mis-submission.
    assert sf.classify_question("Are you authorized to work in Canada?") is None
    assert sf.classify_question(
        "Are you authorized to work in the United States?"
    ) == "work_authorization"


def test_role_specific_questions_never_become_portable_facts():
    for question in (
        "Do you have hands-on experience with Kubernetes?",
        "Tell me about a time you led a migration.",
        "How many years have you worked with Terraform?",
        "Describe your experience with FDA-regulated environments.",
    ):
        assert sf.classify_question(question) is None, question


# ── is_role_specific_question ────────────────────────────────────────────

def test_role_specific_rule_matches_engagement_sanitizer():
    # Mirrors _sanitize_pre_screen_questions_for_pair: past the front-matter
    # block (order_index > 9) or outside default/logistics.
    assert sf.is_role_specific_question("default", 3) is False
    assert sf.is_role_specific_question("logistics", 9) is False
    assert sf.is_role_specific_question("default", 10) is True
    assert sf.is_role_specific_question("skills", 2) is True
    # Case-insensitive: a capitalized stored category must not read as
    # role-specific, or the two functions disagree about the same question.
    assert sf.is_role_specific_question("Default", 3) is False
    assert sf.is_role_specific_question("Logistics", 4) is False


# ── extraction ───────────────────────────────────────────────────────────

def test_facts_extracted_from_webhook_transcriptions():
    facts = sf.facts_from_webhook_payload({
        "interview": {"status": "completed"},
        "transcriptions": [
            {"question": "Are you authorized to work indefinitely for any employer "
                         "in the United States?", "answer": "Yes, I'm a citizen"},
            {"question": "Do you have hands-on experience with Kubernetes?",
             "answer": "Four years"},
            {"question": "What is your current location?", "answer": "Dallas, TX"},
        ],
    })
    by_key = {f.fact_key: f for f in facts}
    assert set(by_key) == {"work_authorization", "current_location"}
    assert by_key["work_authorization"].normalized == {"bool": True}
    assert by_key["current_location"].fact_value == "Dallas, TX"


def test_facts_extracted_from_nested_dashboard_shape():
    # The same blob arrives nested under `data` when it came via the PAIR
    # dashboard proxy rather than the live webhook.
    facts = sf.facts_from_webhook_payload({
        "data": {
            "questions_answers": [
                {"question_text": "Will you now or in the future require visa "
                                  "sponsorship to continue working in the United "
                                  "States?", "answer_text": "No"},
            ]
        }
    })
    assert [f.fact_key for f in facts] == ["visa_sponsorship"]
    assert facts[0].normalized == {"bool": False}


def test_blank_answers_produce_no_fact():
    facts = sf.facts_from_webhook_payload({
        "transcriptions": [
            {"question": "What is your current location?", "answer": "   "},
        ]
    })
    assert facts == []


def test_first_answer_wins_on_duplicate_questions():
    facts = sf.facts_from_qa([
        ("What is your current location?", "Dallas, TX"),
        ("What is your current location?", "Austin, TX"),
    ])
    assert [f.fact_value for f in facts] == ["Dallas, TX"]


def test_unparseable_payload_returns_empty():
    assert sf.facts_from_webhook_payload(None) == []
    assert sf.facts_from_webhook_payload("not a dict") == []
    assert sf.facts_from_webhook_payload({}) == []


def test_eval_map_preserves_legacy_camelcase_keys():
    # The candidate-details UI reads these exact keys; the spec table replaced an
    # inline if/elif chain and must not change the response shape.
    out = sf.eval_map_from_qa([
        {"question_text": "What is your current location?", "answer_text": "Dallas"},
        {"question_text": "Are you authorized to work indefinitely for any employer "
                          "in the United States?", "answer_text": "Yes"},
        {"question_text": "What are your compensation expectations?",
         "answer_text": "$70/hr"},
    ])
    assert out == {
        "currentLocation": "Dallas",
        "isAuthorizedToWorkInUS": "Yes",
        "expectedPayRate": "$70/hr",
    }


# ── freshness ────────────────────────────────────────────────────────────

def test_fresh_and_expired_facts():
    assert _stored("work_authorization", age_days=10).is_fresh(NOW) is True
    # work_authorization TTL is 180d.
    assert _stored("work_authorization", age_days=200).is_fresh(NOW) is False
    # availability TTL is 21d — deliberately much shorter.
    assert _stored("availability", age_days=30).is_fresh(NOW) is False


def test_fact_with_no_expiry_is_never_fresh():
    stale = sf.StoredFact("availability", "Yes", {}, NOW, None, None)
    assert stale.is_fresh(NOW) is False


def test_stale_or_missing_reports_both_causes():
    facts = {
        "work_authorization": _stored("work_authorization", age_days=5),
        "availability": _stored("availability", age_days=60),   # expired
        # actively_looking absent entirely
    }
    stale = sf.stale_or_missing(
        facts, ["work_authorization", "availability", "actively_looking"], now=NOW
    )
    assert stale == ["availability", "actively_looking"]


def test_facts_fresh_until_is_the_earliest_portable_expiry():
    facts = {
        "work_authorization": _stored("work_authorization", age_days=0),   # +180d
        "expected_pay_rate": _stored("expected_pay_rate", age_days=0),     # +60d
    }
    assert sf.facts_fresh_until(facts) == NOW + timedelta(days=60)


def test_facts_fresh_until_ignores_non_portable_facts():
    # availability is not portable, so it must not drag the freshness window down.
    facts = {"availability": _stored("availability", age_days=0)}
    assert sf.facts_fresh_until(facts) is None


def test_portable_keys_exclude_interest():
    # Interest in a specific role is never reusable — that is the whole reason the
    # re-check tier exists rather than skipping outreach outright.
    assert "actively_looking" not in sf.PORTABLE_FACT_KEYS
    assert "work_authorization" in sf.PORTABLE_FACT_KEYS


# ── re-check question generation ─────────────────────────────────────────

def test_recheck_questions_are_front_matter_hard_filters():
    questions = sf.recheck_questions_for(["work_authorization", "availability"])
    assert len(questions) == 2
    for q in questions:
        # order_index must stay inside the front-matter block: PAIR historically
        # ignores hard filters past question 9, and boolean_mode only honours them
        # for questions the sanitizer treats as front matter.
        assert q["order_index"] <= 9
        assert q["category"] == "logistics"
        assert q["is_hard_filter"] is True
        assert sf.is_role_specific_question(q["category"], q["order_index"]) is False


def test_recheck_questions_leave_order_index_1_for_the_interest_question():
    questions = sf.recheck_questions_for(["work_authorization"])
    assert min(q["order_index"] for q in questions) >= 2


def test_unknown_fact_key_is_skipped_not_crashed():
    assert sf.recheck_questions_for(["not_a_real_fact"]) == []
