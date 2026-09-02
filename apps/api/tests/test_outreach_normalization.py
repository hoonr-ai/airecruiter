from services.outreach_normalization import normalize_channel, normalize_phase


def test_normalize_phase_standard_and_aliases():
    assert normalize_phase("phase1") == "phase1"
    assert normalize_phase("PHASE 2") == "phase2"
    assert normalize_phase("stage3") == "phase3"
    assert normalize_phase("3") == "phase3"
    assert normalize_phase("contact_check") == "phase1"
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
