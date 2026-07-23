"""Unit tests for services/location_type.py — the shared work-arrangement
detection used by jobdiva.py (job import) and campaigns.py (child-job seed).

Pins the 2026-07 regression fix: a plain \\bremote\\b search also matched the
word inside negations ("this is NOT a remote role"), so the "negated and not
mentioned" override could never fire and negated-remote jobs stayed Remote —
which then disabled the location gate in unified search (remote jobs search
US-wide and skip the radius verdict).
"""
import os
import sys
from pathlib import Path

# Config requires these at import time; set dummies before importing anything.
for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY",
):
    os.environ.setdefault(_k, "test")

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from services.location_type import (  # noqa: E402
    detect_remote_signals,
    resolve_location_type,
)


# ── detect_remote_signals ─────────────────────────────────────────────────

def test_positive_mention():
    assert detect_remote_signals("this is a fully remote role") == (True, False, True)


def test_negated_mention_is_not_a_positive_mention():
    # THE regression: "remote" inside the negation must not count as a mention.
    mention, negated, has_remote = detect_remote_signals("this is not a remote role")
    assert mention is False
    assert negated is True
    assert has_remote is False


def test_no_remote_work():
    assert detect_remote_signals("no remote work allowed") == (False, True, False)


def test_wfh_slash_remote_compound_negation():
    # "not a WFH/remote role" — the compound must be consumed whole; matching
    # just "wfh" would leave "/remote" behind as a false positive mention.
    assert detect_remote_signals("this is not a wfh/remote position") == (False, True, False)


def test_bare_wfh_negation():
    assert detect_remote_signals("no wfh for this role") == (False, True, False)


def test_positive_and_negated_mentions_veto():
    # Separate positive mention survives as `mention`, but the conservative
    # has_remote flag stays False when any negation is present.
    mention, negated, has_remote = detect_remote_signals(
        "remote work available for FTEs, but not remote for contractors"
    )
    assert mention is True
    assert negated is True
    assert has_remote is False


def test_empty_text():
    assert detect_remote_signals("") == (False, False, False)


# ── resolve_location_type: API says Remote ────────────────────────────────

def test_api_remote_jd_negates_remote_flips_onsite():
    # Was stuck "Remote" before the fix — the negation branch was dead code.
    assert resolve_location_type(
        "Remote",
        "This is not a remote role. Candidate must work onsite 5 days a week.",
    ) == "Onsite"


def test_api_remote_jd_onsite_only_flips_onsite():
    # JD affirms onsite and never mentions remote — JD overrides the API's
    # known-bad "Remote" default.
    assert resolve_location_type("Remote", "Onsite role in Dallas, TX.") == "Onsite"


def test_api_remote_jd_affirms_remote_stays_remote():
    assert resolve_location_type("Remote", "Fully remote position, US-based.") == "Remote"


def test_api_remote_silent_jd_stays_remote():
    # No JD signal → trust the API field (a silent JD must NOT flip a real
    # remote job to Onsite).
    assert resolve_location_type("Remote", "Great role on a growing team.") == "Remote"


def test_api_remote_incidental_onsite_with_positive_remote_stays_remote():
    # A positive remote mention protects against incidental onsite wording.
    assert resolve_location_type(
        "Remote", "This position is remote. Occasional onsite meetings quarterly."
    ) == "Remote"


def test_api_remote_hybrid_jd_flips_hybrid():
    assert resolve_location_type(
        "Remote", "This is a hybrid role: 3 days in office."
    ) == "Hybrid"


def test_api_remote_hybrid_tech_phrase_not_a_work_signal():
    assert resolve_location_type(
        "Remote", "Experience with hybrid cloud environments required. Fully remote."
    ) == "Remote"


def test_api_wfh_alias_maps_to_remote():
    assert resolve_location_type("WFH", "") == "Remote"


# ── resolve_location_type: other API values / silent field ────────────────

def test_api_onsite_jd_agrees():
    assert resolve_location_type("Onsite", "candidate will work onsite daily") == "Onsite"


def test_empty_api_jd_remote():
    assert resolve_location_type("", "100% remote role") == "Remote"


def test_empty_api_jd_onsite():
    assert resolve_location_type("", "onsite position in Austin") == "Onsite"


def test_empty_api_jd_both_implies_hybrid():
    assert resolve_location_type(
        "", "onsite work at the office with some remote days"
    ) == "Hybrid"


def test_empty_api_negated_remote_falls_to_onsite():
    assert resolve_location_type(
        "", "This is not a remote position; work on site required."
    ) == "Onsite"


def test_all_silent_returns_empty():
    # Caller applies its own default (historically "Onsite").
    assert resolve_location_type("", "") == ""


def test_employment_contaminated_field_returns_empty():
    assert resolve_location_type("Direct Placement", "") == ""


def test_api_hybrid_silent_jd():
    assert resolve_location_type("Hybrid", "") == "Hybrid"
