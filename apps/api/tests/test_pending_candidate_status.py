from routers.candidates import _format_engage_status, _is_engage_done

def test_pending_status_for_failed_outreach_without_score():
    # Test _format_engage_status helper directly
    assert _format_engage_status('failed', None, '') == 'Pending'
    assert _format_engage_status('failed', 0.0, '') == 'Fail'
    assert _format_engage_status('fail', None, '') == 'Pending'
    assert _format_engage_status('fail', 80.0, '') == 'Fail'
    assert _format_engage_status('completed', None, 'passed') == 'Pass'
    assert _format_engage_status('completed', None, 'failed') == 'Fail'

def test_is_engage_done_logic():
    # Test _is_engage_done helper directly for both boolean and non-boolean jobs
    # Non-boolean jobs (is_boolean_job = False) require engage_score is not None
    assert _is_engage_done('completed', None, False) is False
    assert _is_engage_done('completed', 85.0, False) is True
    assert _is_engage_done('failed', None, False) is False
    assert _is_engage_done('failed', 0.0, False) is True

    # Boolean jobs (is_boolean_job = True) do NOT require engage_score is not None
    assert _is_engage_done('completed', None, True) is True
    assert _is_engage_done('failed', None, True) is True
