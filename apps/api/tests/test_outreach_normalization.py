from services.outreach_normalization import (
    normalize_channel,
    normalize_phase,
    promote_high_score_extra_phase,
)


def test_normalize_phase_standard_and_aliases():
    assert normalize_phase("contact_check") == "contact_check"
    assert normalize_phase("contact check") == "contact_check"
    assert normalize_phase("phase1") == "phase1"
    assert normalize_phase("Phase 1") == "phase1"
    assert normalize_phase("phase1_6hr") == "phase1_6hr"
    assert normalize_phase("PHASE 2") == "phase1_6hr"
    assert normalize_phase("6hr") == "phase1_6hr"
    assert normalize_phase("phase2") == "phase2"
    assert normalize_phase("Phase 3") == "phase2"
    assert normalize_phase("phase3") == "phase3"
    assert normalize_phase("Phase 4") == "phase3"
    assert normalize_phase("stage3") == "phase3"
    assert normalize_phase("phase1_extra") == "phase1_extra"
    assert normalize_phase("Extra Outreach Phase 1") == "phase1_extra"
    assert normalize_phase("phase1_6hr_extra") == "phase1_6hr_extra"
    assert normalize_phase("Extra Outreach Phase 2") == "phase1_6hr_extra"
    assert normalize_phase("phase2_extra") == "phase2_extra"
    assert normalize_phase("Extra Outreach Phase 3") == "phase2_extra"
    assert normalize_phase("high_score_extra") == "phase1_extra"
    assert normalize_phase("Extra 2") == "phase1_6hr_extra"
    assert normalize_phase("unknown_phase") is None
    assert normalize_phase(None) is None


def test_normalize_phase_allow_pending_aliases():
    assert normalize_phase("contact_check", allow_pending_aliases=True) == "contact_check"
    assert normalize_phase("contact_check", allow_pending_aliases=False) is None
    assert normalize_phase("queued", allow_pending_aliases=True) == "contact_check"
    assert normalize_phase("queued", allow_pending_aliases=False) is None
    assert normalize_phase("scheduled", allow_pending_aliases=False) is None
    assert normalize_phase("not_started", allow_pending_aliases=False) is None
    # Real phases should not be suppressed by allow_pending_aliases=False
    assert normalize_phase("phase1", allow_pending_aliases=False) == "phase1"
    assert normalize_phase("phase1_6hr", allow_pending_aliases=False) == "phase1_6hr"
    assert normalize_phase("phase2", allow_pending_aliases=False) == "phase2"
    assert normalize_phase("phase3", allow_pending_aliases=False) == "phase3"


def test_promote_high_score_extra_phase_from_processing_job():
    payload = {
        "outreach_phase": "phase1",
        "scheduled_jobs": [
            {
                "status": "processing",
                "payload": {
                    "is_high_score_extra": True,
                    "high_score_phase": "phase1",
                    "reminder_type": "high_score_extra",
                },
            }
        ],
    }
    assert promote_high_score_extra_phase(payload, "phase1") == "phase1_extra"


def test_promote_high_score_extra_does_not_override_later_base_phase():
    payload = {
        "outreach_phase": "phase1_6hr",
        "scheduled_jobs": [
            {
                "status": "processing",
                "payload": {
                    "is_high_score_extra": True,
                    "high_score_phase": "phase1",
                    "reminder_type": "high_score_extra",
                },
            }
        ],
    }
    assert promote_high_score_extra_phase(payload, "phase1_6hr") == "phase1_6hr"


def test_promote_high_score_extra_from_communication_phase():
    payload = {
        "outreach_phase": "phase1",
        "communications": [{"phase": "phase1_extra", "channel": "sms"}],
    }
    assert promote_high_score_extra_phase(payload, "phase1") == "phase1_extra"


def test_promote_high_score_extra_skips_missing_high_score_phase():
    payload = {
        "outreach_phase": "phase1",
        "scheduled_jobs": [
            {
                "status": "processing",
                "payload": {
                    "is_high_score_extra": True,
                    "reminder_type": "high_score_extra",
                },
            }
        ],
    }
    assert promote_high_score_extra_phase(payload, "phase1") == "phase1"


def test_promote_high_score_extra_phase2_from_matching_job():
    payload = {
        "outreach_phase": "phase1_6hr",
        "scheduled_jobs": [
            {
                "status": "processing",
                "payload": {
                    "is_high_score_extra": 1,
                    "high_score_phase": "phase1_6hr",
                    "reminder_type": "high_score_extra",
                },
            }
        ],
    }
    assert promote_high_score_extra_phase(payload, "phase1_6hr") == "phase1_6hr_extra"


def test_promote_high_score_extra_dedupes_nested_scheduled_jobs():
    job = {
        "status": "pending",
        "job_type": "reminder",
        "payload": {
            "is_high_score_extra": True,
            "high_score_phase": "phase1",
            "reminder_type": "high_score_extra",
        },
    }
    payload = {
        "outreach_phase": "phase1",
        "scheduled_jobs": [job],
        "outreach": {"scheduled_jobs": [job]},
    }
    assert promote_high_score_extra_phase(payload, "phase1") == "phase1_extra"


def test_promote_extra3_from_phase2_extra_communications():
    """outreach-status omits completed Extra jobs; sent comms still carry phase2_extra."""
    payload = {
        "outreach_phase": "phase2",
        "communications": [
            {"phase": "phase2", "channel": "email"},
            {"phase": "phase2_extra", "channel": "sms"},
        ],
    }
    assert promote_high_score_extra_phase(payload, "phase2") == "phase2_extra"


def test_promote_extra3_from_phase2_high_score_job_without_stored_match_required():
    """PairBot SQL promotes from high_score_phase=phase2 even when stored lags."""
    payload = {
        "outreach_phase": "phase1",
        "scheduled_jobs": [
            {
                "status": "processing",
                "payload": {
                    "is_high_score_extra": True,
                    "high_score_phase": "phase2",
                    "reminder_type": "high_score_extra",
                },
            }
        ],
    }
    assert promote_high_score_extra_phase(payload, "phase1") == "phase2_extra"


def test_promote_extra3_from_events_when_jobs_omitted():
    payload = {
        "outreach_phase": "phase2",
        "events": [{"phase": "phase2_extra", "activity_type": "email_sent"}],
    }
    assert promote_high_score_extra_phase(payload, "phase2") == "phase2_extra"


def test_promote_uses_normalized_alias_for_anti_regression():
    """Raw aliases like 'phase 3' must rank as phase2, not as contact_check (0)."""
    payload = {
        "outreach_phase": "phase 3",
        "scheduled_jobs": [
            {
                "status": "processing",
                "payload": {
                    "is_high_score_extra": True,
                    "high_score_phase": "phase1",
                    "reminder_type": "high_score_extra",
                },
            }
        ],
    }
    # phase 3 → phase2 (rank 50); Extra 1 (rank 20) must not win.
    assert promote_high_score_extra_phase(payload, "phase 3") == "phase2"


def test_promote_pending_extra_outranks_lower_comms_extra():
    """A higher-ranked pending Extra must not lose to a lower confirmed Extra."""
    payload = {
        "outreach_phase": "phase1",
        "communications": [{"phase": "phase1_extra", "channel": "sms"}],
        "scheduled_jobs": [
            {
                "status": "pending",
                "payload": {
                    "is_high_score_extra": True,
                    "high_score_phase": "phase2",
                    "reminder_type": "high_score_extra",
                },
            }
        ],
    }
    assert promote_high_score_extra_phase(payload, "phase1") == "phase2_extra"


def test_normalize_channel_standard_and_aliases():
    assert normalize_channel("call") == "call"
    assert normalize_channel("voice") == "call"
    assert normalize_channel("phone") == "call"
    assert normalize_channel("sms") == "sms"
    assert normalize_channel("whatsapp") == "sms"
    assert normalize_channel("email") == "web"
    assert normalize_channel("mail") == "web"
    assert normalize_channel("web") == "web"
    assert normalize_channel("unknown_channel") is None
    assert normalize_channel(None) is None
