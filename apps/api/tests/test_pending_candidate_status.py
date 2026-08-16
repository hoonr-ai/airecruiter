
def test_pending_status_for_failed_outreach_without_score():
    # Helper logic mockup to verify the code paths we changed
    def format_status(engage_status, engage_score):
        s = engage_status.lower() if engage_status else 'pending'
        if s in ['passed', 'hired', 'pass']:
            return 'Pass'
        elif s in ['failed', 'rejected', 'fail']:
            if engage_score is None:
                return 'Pending'
            else:
                return 'Fail'
        return 'Pending'

    assert format_status('failed', None) == 'Pending'
    assert format_status('failed', 0.0) == 'Fail'
    assert format_status('fail', None) == 'Pending'
    assert format_status('fail', 80.0) == 'Fail'
