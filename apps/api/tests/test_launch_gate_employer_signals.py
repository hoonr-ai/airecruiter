"""Regression tests for the launch gate's view of a candidate's employer.

A candidate currently employed at Bank of America was contacted for — and
passed a phone screen on — a Bank of America req, despite
`is_candidate_excluded_from_pair` having an "Employed by Hiring Client" rule.

The matcher was never at fault: it was handed an empty candidate. The gate read
its employer signals only from `sourced_candidates.data`, but on the JobDiva
applicant auto-launch path that blob carries none of them —
`synchronize_job_applicants` runs with bypass_screening=True (no LLM
enrichment), and `_build_candidate_payload` persisted no employer fields for a
new insert. The extracted employment history lives in `candidate_enhanced_info`
and was only ever joined in memory at read time, so the UI rendered the
conflict the gate could not see.

These tests pin both halves: the gate excludes once hydrated, and the fields it
needs survive the persist round-trip.
"""
from typing import Any, Dict

from routers.engagement import (  # noqa: E402
    _fetch_stored_employer_signals,
    _merge_employer_signals,
    is_candidate_excluded_from_pair,
)
from services.company_match import is_placeholder_client  # noqa: E402


class _FakeCursor:
    """Minimal RealDictCursor stand-in: returns dict rows, supports `with`."""

    def __init__(self, rows, raises=False):
        self._rows, self._raises = rows, raises
        self.executed_ids = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _sql, params=None):
        if self._raises:
            raise RuntimeError("relation \"candidate_enhanced_info\" does not exist")
        self.executed_ids = params[0] if params else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows, raises=False):
        self.cursor_obj = _FakeCursor(rows, raises)

    def cursor(self, **_kwargs):
        return self.cursor_obj

CLIENT = "Bank of America"

# Exactly what _build_candidate_payload persists for a NEW insert, before any
# resume extraction has run.
BARE_BLOB: Dict[str, Any] = {
    "skills": ["Selenium"],
    "experience_years": 6,
    "education": [],
    "certifications": [],
    "company_experience": [],
    "urls": {},
    "is_selected": True,
    "match_score": 0,
    "enhanced_info": None,
    "auto_assigned": True,
    "jobdiva_candidate_id": "999",
}

# What candidate_enhanced_info holds for her once the resume is extracted.
STORED_SIGNALS: Dict[str, Any] = {
    "company_experience": [
        {
            "company": "Bank Of America",
            "title": "QA Automation Engineer",
            "start_date": "3/2020",
            "end_date": "Present",
        }
    ],
    "title": "QA Automation Engineer",
}


# ── the incident ──────────────────────────────────────────────────────────

def test_bare_blob_is_the_bug_not_a_clean_candidate():
    """The un-hydrated blob carries no employer signal — the gate is blind."""
    assert is_candidate_excluded_from_pair(BARE_BLOB, CLIENT) == (False, "")


def test_hydrated_candidate_is_excluded_from_hiring_client():
    hydrated = _merge_employer_signals(BARE_BLOB, signals=STORED_SIGNALS)
    assert is_candidate_excluded_from_pair(hydrated, CLIENT) == (
        True,
        "Employed by Hiring Client",
    )


def test_headline_only_row_is_excluded():
    """Proves the `headline` column the gate queries now select is wired in.

    collect_current_companies has an "… at X" parse for rows that carry the
    employer only in headline text; it could never fire while neither launch
    query selected the column.
    """
    hydrated = _merge_employer_signals(
        BARE_BLOB, headline="QA Engineer at Bank of America"
    )
    assert is_candidate_excluded_from_pair(hydrated, CLIENT) == (
        True,
        "Employed by Hiring Client",
    )


# ── the fix must not over-exclude ─────────────────────────────────────────

def test_unrelated_client_is_not_excluded():
    hydrated = _merge_employer_signals(BARE_BLOB, signals=STORED_SIGNALS)
    assert is_candidate_excluded_from_pair(hydrated, "Wells Fargo") == (False, "")


def test_named_current_employer_beats_a_client_stint_in_the_past():
    """The bar is present employment. Someone who left the client for a named
    employer is contactable — the last employer is a FALLBACK, not a union."""
    moved_on = {
        "company_experience": [
            {"company": "Bank Of America", "start_date": "3/2018", "end_date": "2/2024"},
            {"company": "Stripe", "end_date": "Present"},
        ]
    }
    hydrated = _merge_employer_signals(BARE_BLOB, signals=moved_on)
    assert is_candidate_excluded_from_pair(hydrated, CLIENT) == (False, "")


def test_last_employer_counts_when_no_current_one_is_on_file():
    """Many source rows carry a history with no "Present" entry. There the most
    recent employer is the best available answer to "where do they work now?"."""
    no_current = {
        "company_experience": [
            {"company": "Bank Of America", "start_date": "3/2018", "end_date": "2/2024"}
        ]
    }
    hydrated = _merge_employer_signals(BARE_BLOB, signals=no_current)
    assert is_candidate_excluded_from_pair(hydrated, CLIENT) == (
        True,
        "Employed by Hiring Client (last known employer)",
    )


def test_old_history_never_reaches_back_past_the_most_recent_employer():
    """Only the single most recent past employer is consulted, so a stint at
    the client years ago cannot block someone."""
    ancient = {
        "company_experience": [
            {"company": "Bank Of America", "start_date": "2005", "end_date": "2008"},
            {"company": "Google", "start_date": "2008", "end_date": "2/2024"},
        ]
    }
    hydrated = _merge_employer_signals(BARE_BLOB, signals=ancient)
    assert is_candidate_excluded_from_pair(hydrated, CLIENT) == (False, "")


def test_placeholder_client_never_excludes():
    """'Unknown Customer' is the JobDiva sync's own fallback for a req with no
    customer, so it must not behave as a real client — otherwise a candidate
    whose company is literally 'Unknown' token-matches it."""
    assert is_placeholder_client("Unknown Customer")
    assert is_candidate_excluded_from_pair(
        _merge_employer_signals(BARE_BLOB, signals=STORED_SIGNALS), "Unknown Customer"
    ) == (False, "")
    assert is_candidate_excluded_from_pair(
        {"current_company": "Unknown"}, "Unknown Customer"
    ) == (False, "")


# ── the candidate_enhanced_info lookup ────────────────────────────────────

def test_fetch_signals_keys_by_candidate_id():
    conn = _FakeConn([
        {
            "candidate_id": 999,  # JobDiva ids arrive numeric; keys must be str
            "job_title": "QA Automation Engineer",
            "company_experience": STORED_SIGNALS["company_experience"],
        }
    ])
    got = _fetch_stored_employer_signals(conn, ["999"])
    assert got["999"]["company_experience"][0]["company"] == "Bank Of America"
    assert got["999"]["title"] == "QA Automation Engineer"


def test_fetch_signals_skips_blank_ids_and_short_circuits():
    conn = _FakeConn([])
    assert _fetch_stored_employer_signals(conn, ["", "  ", None]) == {}
    assert conn.cursor_obj.executed_ids is None  # never hit the DB


def test_fetch_signals_never_raises_into_the_launch_path():
    """A hydration failure must degrade to a blind gate, not abort the launch."""
    assert _fetch_stored_employer_signals(_FakeConn([], raises=True), ["999"]) == {}


def test_fetch_signals_tolerates_null_columns():
    conn = _FakeConn([
        {"candidate_id": "999", "job_title": None, "company_experience": None}
    ])
    assert _fetch_stored_employer_signals(conn, ["999"]) == {
        "999": {"company_experience": [], "title": ""}
    }


# ── the client_conflict flag shown in the candidate list ──────────────────

def test_flag_stamps_reason_and_relation():
    from services.company_match import apply_client_conflict_flag

    cand = {"company_experience": [{"company": "Bank Of America", "end_date": "Present"}]}
    assert apply_client_conflict_flag(cand, CLIENT) is True
    assert cand["client_conflict"] is True
    assert cand["client_conflict_relation"] == "current"
    assert cand["client_conflict_company"] == "Bank Of America"
    assert "hiring client" in cand["client_conflict_reason"]


def test_flag_wording_distinguishes_the_fallback():
    from services.company_match import apply_client_conflict_flag

    cand = {"company_experience": [{"company": "Bank Of America", "end_date": "2/2024"}]}
    apply_client_conflict_flag(cand, CLIENT)
    assert cand["client_conflict_relation"] == "last"
    assert "No current employer on file" in cand["client_conflict_reason"]


def test_flag_self_heals_against_a_different_job():
    """The conflict is per-job, so a flag from another req must clear rather
    than silently blocking the candidate everywhere."""
    from services.company_match import apply_client_conflict_flag

    cand = {"company_experience": [{"company": "Bank Of America", "end_date": "Present"}]}
    apply_client_conflict_flag(cand, CLIENT)
    assert apply_client_conflict_flag(cand, "Wells Fargo") is False
    assert cand["client_conflict"] is False
    assert "client_conflict_reason" not in cand


def test_flag_never_set_for_placeholder_client():
    from services.company_match import apply_client_conflict_flag

    cand = {"company_experience": [{"company": "Bank Of America", "end_date": "Present"}]}
    assert apply_client_conflict_flag(cand, "Unknown Customer") is False


# ── merge semantics ───────────────────────────────────────────────────────

def test_merge_does_not_mutate_the_stored_blob():
    blob = dict(BARE_BLOB)
    _merge_employer_signals(blob, headline="Eng at Stripe", signals=STORED_SIGNALS)
    assert blob == BARE_BLOB


def test_merge_fills_only_absent_fields():
    """A value already on the row wins over the cached one, which can be up to
    30 days stale and is keyed globally rather than per job."""
    row = {
        "company_experience": [{"company": "Stripe", "end_date": "Present"}],
        "headline": "Engineer at Stripe",
    }
    merged = _merge_employer_signals(
        row, headline="Engineer at Google", signals=STORED_SIGNALS
    )
    assert merged["company_experience"] == row["company_experience"]
    assert merged["headline"] == "Engineer at Stripe"


def test_merge_tolerates_missing_signals():
    assert _merge_employer_signals(BARE_BLOB, signals=None) == BARE_BLOB
    assert _merge_employer_signals(BARE_BLOB) == BARE_BLOB


# ── the other four exclusion reasons must survive the persist round-trip ──

def _persisted(applicant: Dict[str, Any]) -> Dict[str, Any]:
    from services.auto_assign_service import auto_assign_service

    return auto_assign_service._build_candidate_payload(applicant, "999", None)


def test_available_false_survives_and_excludes():
    """`available` is meaningfully False, so it must be copied with an explicit
    None test — an `or` chain silently discards it."""
    persisted = _persisted({"candidate_id": "999", "available": False})
    assert persisted["available"] is False
    assert is_candidate_excluded_from_pair(persisted, CLIENT) == (
        True,
        "Current Employee (Pyramid / Unavailable)",
    )


def test_employee_status_survives_and_excludes():
    persisted = _persisted(
        {"candidate_id": "999", "employee_status": "Current Employee"}
    )
    assert is_candidate_excluded_from_pair(persisted, CLIENT) == (
        True,
        "Current Employee (Pyramid)",
    )


def test_qualifications_survive_and_exclude():
    persisted = _persisted(
        {
            "candidate_id": "999",
            "qualifications": [{"qualificationValue": "Offer Accepted"}],
        }
    )
    assert is_candidate_excluded_from_pair(persisted, CLIENT) == (True, "Offer Accepted")


def test_clean_applicant_still_launches():
    persisted = _persisted(
        {"candidate_id": "999", "available": True, "title": "QA Engineer"}
    )
    assert is_candidate_excluded_from_pair(persisted, CLIENT) == (False, "")
