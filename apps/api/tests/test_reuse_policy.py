"""Unit tests for the reuse tier decision and the pool bucket computation.

`decide_tier` and `compute_bucket` are pure functions precisely so they can be
tested without a database — the DB-touching wrappers around them only assemble
inputs.

Policy under test (the four choices this feature was configured with):
  * re-check by default; skip outreach only for a pass within 14 days at the
    same client
  * logistics facts are portable across clients, interest never is — so a
    cross-client screen caps at re-check
  * 14-day cooldown, 10-day no-response, 3 attempts per rolling 90 days
  * failure direction is always *more* outreach, never less
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

from services import reuse_policy as rp  # noqa: E402
from services import screen_facts as sf  # noqa: E402
from services.candidate_pool import (  # noqa: E402
    PersonOutreachContext,
    compute_bucket,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

THIS_JOB = rp.JobContext(
    jobdiva_id="26-50001",
    job_id="900001",
    title="Senior Platform Engineer",
    customer_name="Acme Health",
    screening_level="L1.5",
)

JOB_META = {
    # Same client as THIS_JOB.
    "26-40001": {"title": "Platform Engineer", "customer_name": "Acme Health"},
    # Different client.
    "26-40002": {"title": "Backend Engineer", "customer_name": "Globex"},
}


def _facts(*fact_keys, age_days=1):
    """Fresh stored facts for the given keys."""
    out = {}
    for key in fact_keys:
        spec = sf.spec_for(key)
        captured = NOW - timedelta(days=age_days)
        out[key] = sf.StoredFact(
            fact_key=key,
            fact_value="Yes",
            normalized={"bool": True},
            captured_at=captured,
            expires_at=captured + timedelta(days=spec.ttl_days),
            jobdiva_id="26-40001",
        )
    return out


ALL_FRESH = lambda age_days=1: _facts(  # noqa: E731
    *set(sf.PORTABLE_FACT_KEYS) | set(sf.TIER_C_REQUIRED_FACT_KEYS), age_days=age_days
)


def _ctx(**kw):
    kw.setdefault("person_id", 7)
    return PersonOutreachContext(**kw)


def _decide(ctx, facts=None, job=THIS_JOB, person_id=7):
    return rp.decide_tier(
        candidate_id="c1",
        person_id=person_id,
        job=job,
        ctx=ctx,
        facts=facts if facts is not None else {},
        job_meta=JOB_META,
        now=NOW,
    )


# ── unknown / no history ─────────────────────────────────────────────────

def test_unknown_person_gets_a_full_screen():
    d = _decide(None, person_id=None)
    assert d.tier == rp.TIER_FULL
    assert d.reason_code == "unknown_person"


def test_never_contacted_person_gets_a_full_screen():
    d = _decide(_ctx())
    assert d.tier == rp.TIER_FULL


def test_attempts_but_no_completed_screen_gets_a_full_screen():
    # Contacted long ago, never answered, past cooldown: start over rather than
    # sending a re-check that references a conversation that never happened.
    d = _decide(_ctx(
        last_sent_at=NOW - timedelta(days=40),
        last_status="no_response",
        last_jobdiva_id="26-40001",
        recent_attempt_times=[NOW - timedelta(days=40)],
    ))
    assert d.tier == rp.TIER_FULL
    assert d.reason_code == "no_completed_screen"


# ── suppression ──────────────────────────────────────────────────────────

def test_dnc_suppresses_everything():
    d = _decide(_ctx(
        dnc_blocked=True,
        last_completed_at=NOW - timedelta(days=1),
        last_outcome="pass",
        last_completed_jobdiva_id="26-40001",
    ), facts=ALL_FRESH())
    assert d.tier == rp.TIER_SUPPRESSED
    assert d.reason_code == "dnc"


def test_open_attempt_for_this_job_reads_as_already_launched():
    d = _decide(_ctx(
        last_sent_at=NOW - timedelta(days=2),
        last_status="sent",
        last_jobdiva_id="26-50001",
        has_open_attempt=True,
        open_attempt_jobdiva_id="26-50001",
        recent_attempt_times=[NOW - timedelta(days=2)],
    ))
    assert d.tier == rp.TIER_SUPPRESSED
    assert d.reason_code == "already_launched_this_job"


def test_open_attempt_for_another_job_suppresses_and_names_the_wait():
    # THE core cross-job case: mid-screen elsewhere, so a second job must not
    # call them. Nothing in the pre-existing per-job idempotency check caught
    # this.
    d = _decide(_ctx(
        last_sent_at=NOW - timedelta(days=3),
        last_status="sent",
        last_jobdiva_id="26-40002",
        has_open_attempt=True,
        open_attempt_jobdiva_id="26-40002",
        recent_attempt_times=[NOW - timedelta(days=3)],
    ))
    assert d.tier == rp.TIER_SUPPRESSED
    assert d.reason_code == "awaiting_response_other_job"
    assert "3 days ago" in d.reason
    assert d.prior_jobdiva_id == "26-40002"


def test_cooldown_blocks_recontact_after_silence():
    d = _decide(_ctx(
        last_sent_at=NOW - timedelta(days=5),
        last_status="no_response",
        last_jobdiva_id="26-40001",
        recent_attempt_times=[NOW - timedelta(days=5)],
    ))
    assert d.tier == rp.TIER_SUPPRESSED
    assert d.reason_code == "cooldown"
    assert d.next_eligible_at == NOW - timedelta(days=5) + timedelta(days=14)


def test_cooldown_expires_after_fourteen_days():
    d = _decide(_ctx(
        last_sent_at=NOW - timedelta(days=15),
        last_status="no_response",
        last_jobdiva_id="26-40001",
        recent_attempt_times=[NOW - timedelta(days=15)],
    ))
    assert d.tier != rp.TIER_SUPPRESSED


def test_a_completed_screen_imposes_no_cooldown():
    # They engaged, so re-approaching them for a different role is legitimate —
    # that is what the re-check tier is for. Only silence earns a cooldown.
    ctx = _ctx(
        last_sent_at=NOW - timedelta(days=2),
        last_status="completed",
        last_jobdiva_id="26-40001",
        last_completed_at=NOW - timedelta(days=2),
        last_completed_jobdiva_id="26-40001",
        last_outcome="pass",
        recent_attempt_times=[NOW - timedelta(days=2)],
    )
    assert ctx.cooldown_until() is None
    assert _decide(ctx, facts=ALL_FRESH()).tier != rp.TIER_SUPPRESSED


def test_attempt_cap_suppresses_even_past_cooldown():
    sent = [NOW - timedelta(days=20), NOW - timedelta(days=45), NOW - timedelta(days=70)]
    d = _decide(_ctx(
        last_sent_at=sent[0],
        last_status="completed",
        last_jobdiva_id="26-40001",
        last_completed_at=sent[0],
        last_completed_jobdiva_id="26-40001",
        last_outcome="pass",
        recent_attempt_times=sent,
    ), facts=ALL_FRESH())
    assert d.tier == rp.TIER_SUPPRESSED
    assert d.reason_code == "attempt_cap"
    # The 3rd-most-recent attempt is the one whose 90-day expiry frees a slot.
    assert d.next_eligible_at == sent[2] + timedelta(days=90)


def test_two_attempts_in_window_is_under_the_cap():
    sent = [NOW - timedelta(days=20), NOW - timedelta(days=45)]
    d = _decide(_ctx(
        last_sent_at=sent[0],
        last_status="completed",
        last_jobdiva_id="26-40001",
        last_completed_at=sent[0],
        last_completed_jobdiva_id="26-40001",
        last_outcome="pass",
        recent_attempt_times=sent,
    ), facts=ALL_FRESH())
    assert d.tier != rp.TIER_SUPPRESSED


# ── tier C: skip outreach ────────────────────────────────────────────────

def _passed_same_client(days_ago=3):
    return _ctx(
        last_sent_at=NOW - timedelta(days=days_ago),
        last_status="completed",
        last_jobdiva_id="26-40001",
        last_completed_at=NOW - timedelta(days=days_ago),
        last_completed_jobdiva_id="26-40001",
        last_outcome="pass",
        recent_attempt_times=[NOW - timedelta(days=days_ago)],
    )


def test_recent_pass_at_same_client_skips_outreach():
    d = _decide(_passed_same_client(3), facts=ALL_FRESH())
    assert d.tier == rp.TIER_NONE
    assert d.reason_code == "recent_pass_same_client"
    assert d.prior_score is None or isinstance(d.prior_score, float)
    assert d.prior_is_other_client is False
    assert d.contacts is False


def test_pass_older_than_the_skip_window_falls_back_to_recheck():
    d = _decide(_passed_same_client(20), facts=ALL_FRESH(age_days=20))
    assert d.tier == rp.TIER_RECHECK
    assert d.reason_code == "recheck_stale_screen"


def test_recent_pass_with_a_stale_required_fact_falls_back_to_recheck():
    # Availability expires in 21 days; a 3-day-old pass whose availability answer
    # is 40 days old cannot justify skipping the call.
    facts = ALL_FRESH()
    facts.update(_facts("availability", age_days=40))
    d = _decide(_passed_same_client(3), facts=facts)
    assert d.tier == rp.TIER_RECHECK
    assert d.reason_code == "recheck_stale_facts"
    assert "availability" in d.reason.lower() or "earliest availability" in d.reason.lower()


def test_recent_pass_with_no_facts_at_all_falls_back_to_recheck():
    # A completed screen we have no extracted facts for (pre-backfill, or a
    # transcript we couldn't parse) must not skip outreach.
    d = _decide(_passed_same_client(3), facts={})
    assert d.tier == rp.TIER_RECHECK


def test_recent_fail_never_skips_outreach():
    ctx = _passed_same_client(3)
    ctx.last_outcome = "fail"
    d = _decide(ctx, facts=ALL_FRESH())
    assert d.tier == rp.TIER_RECHECK
    assert d.reason_code == "recheck_after_fail"


# ── cross-client policy ──────────────────────────────────────────────────

def test_cross_client_pass_caps_at_recheck():
    # Logistics answers carry over; interest in a Globex role is not interest in
    # an Acme Health role, so we always re-ask — never skip.
    ctx = _passed_same_client(2)
    ctx.last_completed_jobdiva_id = "26-40002"   # Globex
    ctx.last_jobdiva_id = "26-40002"
    d = _decide(ctx, facts=ALL_FRESH())
    assert d.tier == rp.TIER_RECHECK
    assert d.reason_code == "recheck_other_client"
    assert d.prior_is_other_client is True
    assert "Globex" in d.reason


def test_cross_client_recheck_still_reuses_fresh_logistics_facts():
    ctx = _passed_same_client(2)
    ctx.last_completed_jobdiva_id = "26-40002"
    d = _decide(ctx, facts=ALL_FRESH())
    # Nothing stale => nothing re-asked beyond the interest question.
    assert d.stale_fact_keys == []


def test_recheck_names_only_the_expired_facts():
    facts = ALL_FRESH()
    facts.update(_facts("expected_pay_rate", age_days=90))   # TTL 60d -> expired
    d = _decide(_passed_same_client(20), facts=facts)
    assert d.tier == rp.TIER_RECHECK
    assert d.stale_fact_keys == ["expected_pay_rate"]


def test_unknown_client_on_either_side_is_not_the_same_client():
    ctx = _passed_same_client(2)
    ctx.last_completed_jobdiva_id = "26-99999"   # not in JOB_META
    d = _decide(ctx, facts=ALL_FRESH())
    assert d.tier == rp.TIER_RECHECK


# ── serialization ────────────────────────────────────────────────────────

def test_decision_to_dict_is_json_safe_and_labels_stale_facts():
    facts = ALL_FRESH()
    facts.update(_facts("expected_pay_rate", age_days=90))
    payload = _decide(_passed_same_client(20), facts=facts).to_dict()
    import json

    json.dumps(payload)   # must not raise
    assert payload["stale_fact_labels"] == ["Compensation expectation"]
    assert payload["tier"] == "recheck"


def test_to_dict_redacts_the_prior_job_when_flagged():
    d = _decide(_passed_same_client(20), facts=ALL_FRESH(age_days=20))
    d.prior_job_redacted = True
    payload = d.to_dict()
    assert payload["prior_jobdiva_id"] is None
    assert payload["prior_job_title"] is None
    assert payload["prior_job_redacted"] is True


def test_summarize_counts_every_tier():
    from services.reuse_policy import TierDecision

    counts = rp.summarize([
        TierDecision(candidate_id="a", tier=rp.TIER_FULL),
        TierDecision(candidate_id="b", tier=rp.TIER_RECHECK),
        TierDecision(candidate_id="c", tier=rp.TIER_RECHECK),
        TierDecision(candidate_id="d", tier=rp.TIER_NONE),
        TierDecision(candidate_id="e", tier=rp.TIER_SUPPRESSED),
    ])
    assert counts == {"full": 1, "recheck": 2, "none": 1, "suppressed": 1}


# ── bucket computation ───────────────────────────────────────────────────

def test_bucket_never_contacted():
    assert compute_bucket(_ctx(), now=NOW) == "never_contacted"


def test_bucket_in_progress_within_the_no_response_window():
    ctx = _ctx(last_sent_at=NOW - timedelta(days=4), last_status="sent")
    assert compute_bucket(ctx, now=NOW) == "in_progress"


def test_bucket_flips_to_no_response_past_ten_days():
    # This is what populates the page's bottom section: silence produces no
    # webhook, so age is the only signal.
    ctx = _ctx(last_sent_at=NOW - timedelta(days=11), last_status="sent")
    assert compute_bucket(ctx, now=NOW) == "no_response"


def test_bucket_completed():
    ctx = _ctx(
        last_sent_at=NOW - timedelta(days=30),
        last_status="completed",
        last_completed_at=NOW - timedelta(days=30),
        last_outcome="pass",
    )
    assert compute_bucket(ctx, now=NOW) == "completed"


def test_bucket_follows_the_most_recent_attempt_not_the_completed_one():
    # A person with an old completed screen AND a fresh open attempt reads as
    # in-progress, because the page is organized by recency. Their completed
    # screen is still exposed through last_completed_at for the reuse pool.
    ctx = _ctx(
        last_sent_at=NOW - timedelta(days=1),
        last_status="sent",
        last_completed_at=NOW - timedelta(days=60),
        last_outcome="pass",
    )
    assert compute_bucket(ctx, now=NOW) == "in_progress"


def test_bucket_suppressed_wins_over_everything():
    ctx = _ctx(last_sent_at=NOW - timedelta(days=1), last_status="sent", dnc_blocked=True)
    assert compute_bucket(ctx, now=NOW) == "suppressed"


def test_bucket_no_response_status_stays_no_response():
    ctx = _ctx(last_sent_at=NOW - timedelta(days=200), last_status="no_response")
    assert compute_bucket(ctx, now=NOW) == "no_response"


def test_attempts_90d_counts_only_the_window():
    ctx = _ctx(recent_attempt_times=[NOW - timedelta(days=1), NOW - timedelta(days=80)])
    assert ctx.attempts_90d == 2
