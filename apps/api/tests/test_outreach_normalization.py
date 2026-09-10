from services.outreach_normalization import normalize_channel, normalize_phase


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
    assert normalize_phase("unknown_phase") is None
    assert normalize_phase(None) is None


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
