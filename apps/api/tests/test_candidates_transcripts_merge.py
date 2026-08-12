from routers.candidates import _merge_transcriptions

def test_transcripts_merge_preserves_non_dict_items():
    # Simulate a webhook list (previously saved) and a live list from PairBot
    webhook_list = []
    live_list = [
        "not a dict",
        {"question": "Q1", "answer": "A1"},
        ["also not a dict"]
    ]

    merged = _merge_transcriptions(webhook_list, live_list)
    assert len(merged) == 3
    assert merged[0] == "not a dict"
    assert merged[1]["question"] == "Q1"
    assert merged[2] == ["also not a dict"]

def test_transcripts_merge_patches_hard_filter_status():
    webhook_list = [
        {"question": "Are you a US citizen?", "hard_filter_status": "passed"}
    ]
    live_list = [
        {"question": "Are you a US citizen?", "answer": "Yes"}
    ]

    merged = _merge_transcriptions(webhook_list, live_list)
    assert len(merged) == 1
    assert merged[0]["question"] == "Are you a US citizen?"
    assert merged[0]["hard_filter_status"] == "passed"
