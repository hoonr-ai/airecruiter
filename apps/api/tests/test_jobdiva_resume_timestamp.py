"""Coverage gap flagged on PR #573: `_resume_timestamp` tries DATEUPDATED,
DATECREATED, DATELASTDOWNLOADED, DATEFIRSTDOWNLOADED in order and falls
through to the next key whenever `_parse_jobdiva_datetime` can't parse the
current one. That fallback was untested for the case that actually exercises
it — a non-ISO ("dash-less") value in a field that isn't first in the list.
"""
from datetime import datetime, timezone

from services.jobdiva import JobDivaService

svc = JobDivaService()


def test_parse_jobdiva_datetime_accepts_iso_with_z_suffix():
    parsed = svc._parse_jobdiva_datetime("2026-06-18T02:07:36Z")
    assert parsed == datetime(2026, 6, 18, 2, 7, 36, tzinfo=timezone.utc)


def test_parse_jobdiva_datetime_rejects_dash_less_date():
    """fromisoformat has no dash-less date support before Python 3.11's more
    lenient parser; a compact "20260618" must fail closed (None), not raise."""
    assert svc._parse_jobdiva_datetime("20260618") is None


def test_parse_jobdiva_datetime_handles_empty_and_none():
    assert svc._parse_jobdiva_datetime(None) is None
    assert svc._parse_jobdiva_datetime("") is None


def test_resume_timestamp_falls_through_a_dash_less_first_field():
    """DATEUPDATED (checked first) is dash-less and unparseable; the helper
    must fall through to DATECREATED rather than returning datetime.min."""
    resume = {
        "DATEUPDATED": "20260101",
        "DATECREATED": "2026-05-01T00:00:00Z",
    }
    ts = svc._resume_timestamp(resume)
    assert ts == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_resume_timestamp_defaults_to_min_when_every_field_is_unparseable():
    resume = {"DATEUPDATED": "20260101", "DATECREATED": "not-a-date"}
    assert svc._resume_timestamp(resume) == datetime.min
