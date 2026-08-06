"""Unit tests for core.utils.is_remote_job — the canonical remote-detection function."""
import pytest
from core.utils import is_remote_job


@pytest.mark.parametrize("location_type,city,expected", [
    # Standard remote values
    ("Remote",          "",              True),
    ("remote",          "",              True),
    ("Fully Remote",    "",              True),
    ("Fully-Remote",    "",              True),
    ("fullyremote",     "",              True),
    ("WFH",             "",              True),
    ("wfh",             "",              True),
    # Frontend-only synonyms now covered by backend
    ("Virtual",         "",              True),
    ("virtual",         "",              True),
    ("Telecommute",     "",              True),
    ("telecommute",     "",              True),
    # JobDiva-import edge case: empty location_type, city = "REMOTE"
    ("",                "REMOTE",        True),
    ("On-Site",         "REMOTE",        True),
    # On-site / hybrid — must NOT be treated as remote
    ("On-Site",         "Farmington, MI", False),
    ("Onsite",          "Farmington, MI", False),
    ("Hybrid",          "Farmington, MI", False),
    ("",                "Farmington, MI", False),
    (None,              "",              False),
])
def test_is_remote_job(location_type, city, expected):
    assert is_remote_job(location_type, city) == expected
