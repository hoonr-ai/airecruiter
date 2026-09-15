"""Cross submissions must score on the SAME matrix as Step 5.

The cross-submission scan surfaces people PAIR already screened for other
jobs and emails the recruiter about them, so its match % has to be the
number the recruiter would have seen had that person come back from a Step-5
search. Both paths now go through
`unified_candidate_search.apply_scoring_policy`; these tests pin the pieces
that used to drift:

* the full pipeline, not just `_score_candidate` — the no-contact and
  hiring-client gates and the N/A policies are part of the score;
* the scoring payload carrying everything that pipeline reads (`source`, and
  a title, wherever the title happens to live);
* the criteria carrying the inputs to the pass/fail hard filters;
* an N/A verdict staying N/A instead of being coerced to 0.

No DB: `_build_resume_matching_criteria` is exercised with a mocked
connection, everything else is pure.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from routers.candidates import (
    _build_candidate_for_resume_matching,
    _build_resume_matching_criteria,
    _compute_resume_matching,
)
from services import cross_submissions as cs
from services.unified_candidate_search import (
    SearchCriteria,
    unified_search_service as U,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

# The reported symptom: a "Java Developer" job suggesting Java + QA people.
JAVA_DEV_RUBRIC = SearchCriteria(
    job_id="26-99999",
    title_criteria=[{"value": "Java Developer", "match_type": "must", "similar_terms": []}],
    skill_criteria=[{"value": "Java", "match_type": "must"}],
    location="",
    location_type="Remote",
)

QA_RESUME = "Senior QA Automation Engineer. Java, Selenium WebDriver, TestNG."
QA_SKILLS = ["Java", "Selenium", "TestNG"]


def _step5_candidate(**over):
    cand = {
        "candidate_id": "q1",
        "name": "QA Person",
        "title": "QA Automation Engineer",
        "headline": "QA Automation Engineer",
        "location": "Austin, TX",
        "skills": list(QA_SKILLS),
        "resume_text": QA_RESUME,
        "source": "JobDiva-TalentSearch",
    }
    cand.update(over)
    return cand


def _cross_sub_row(*, headline=None, source="JobDiva-TalentSearch", blob_over=None):
    """A `sourced_candidates` row as `fetch_prior_rows` returns it."""
    blob = {
        "engage_status": "passed",
        "engage_updated_at": (NOW - timedelta(days=5)).isoformat(),
        "skills": list(QA_SKILLS),
        "resume_text": QA_RESUME,
        # Where the title actually lives for many stored rows: in the blob,
        # not the `headline` column.
        "headline": "QA Automation Engineer",
    }
    if blob_over:
        blob.update(blob_over)
    return {
        "id": 1,
        "prior_key": "26-11111",
        "candidate_id": "q1",
        "source": source,
        "name": "QA Person",
        "email": "qa@example.com",
        "phone": "",
        "headline": headline,
        "location": "Austin, TX",
        "resume_text": QA_RESUME,
        "data": json.dumps(blob),
        "resume_match_percentage": 70,
        "updated_at": NOW - timedelta(days=5),
        "prior_job_id": "1001",
        "prior_jobdiva_id": "26-11111",
        "prior_title": "QA Engineer",
        "prior_enhanced_title": None,
        "prior_customer_name": "Acme",
    }


def _score_via_cross_submissions(row, criteria=JAVA_DEV_RUBRIC):
    blob = json.loads(row["data"])
    return _compute_resume_matching(cs.build_scoring_payload(row, blob), criteria)


# ---------------------------------------------------------------------------
# One pipeline, one number
# ---------------------------------------------------------------------------
def test_cross_submission_score_equals_step5_score():
    """Same person, same rubric → the same percentage on both paths."""
    step5 = U.apply_scoring_policy(_step5_candidate(), JAVA_DEV_RUBRIC)["match_score"]
    cross = _score_via_cross_submissions(_cross_sub_row())["score"]
    assert cross == pytest.approx(float(step5))


def test_step5_finalize_delegates_to_the_shared_policy():
    """`finalize_candidate` must not grow a second copy of the pipeline.

    Parity only holds while Step 5 and everything else run the same code, so
    the search's per-candidate hook stays a one-line delegation.
    """
    import inspect

    src = inspect.getsource(U.search_candidates)
    body = src.split("def finalize_candidate(cand):", 1)[1]
    body = body.split("\n\n", 1)[0]
    assert "apply_scoring_policy" in body
    # No scoring logic inline — a stray gate here is how the paths drifted.
    for leaked in ("_score_candidate(", "apply_no_contact_flag", "match_score\"] = None"):
        assert leaked not in body, f"{leaked} belongs in apply_scoring_policy"


# ---------------------------------------------------------------------------
# The title bucket: 15 of the 100 points, and it must be evaluated
# ---------------------------------------------------------------------------
def test_title_is_recovered_from_the_blob_when_the_column_is_empty():
    """A missing title silently DELETES the title bucket from the denominator.

    The matrix normalizes to 100 over whatever it could evaluate, so a
    candidate with no title reaching the scorer is graded on must-have skills
    alone — which is how a Java QA engineer read as a perfect Java developer.
    `headline` is NULL on plenty of stored rows, so the payload has to walk
    the fallbacks.
    """
    row = _cross_sub_row(headline=None)
    result = _score_via_cross_submissions(row)
    not_evaluated = result["score_details"]["normalization"]["not_evaluated"]
    assert not any("Title relevance" in n for n in not_evaluated), not_evaluated
    # And it lands on the same number as the row whose column IS populated.
    populated = _score_via_cross_submissions(
        _cross_sub_row(headline="QA Automation Engineer")
    )
    assert result["score"] == populated["score"]


def test_title_falls_back_to_enhanced_info():
    """`enhanced_info.job_title` is the title for LLM-enhanced rows."""
    cand = _build_candidate_for_resume_matching({
        "candidate_id": "x",
        "data": {"enhanced_info": {"job_title": "QA Automation Engineer"}},
    })
    assert cand["title"] == "QA Automation Engineer"
    assert cand["headline"] == "QA Automation Engineer"


def test_dropping_the_title_bucket_inflates_a_title_mismatch():
    """Why the fallback matters, in points.

    Pins the mechanism rather than the exact numbers: a candidate whose title
    does not match must score STRICTLY LOWER with the title evaluated than
    with the bucket dropped, and strictly lower than a true title match.
    """
    with_title = U._score_candidate(_step5_candidate(), JAVA_DEV_RUBRIC)["score"]
    no_title = U._score_candidate(
        {k: v for k, v in _step5_candidate().items() if k not in ("title", "headline")},
        JAVA_DEV_RUBRIC,
    )["score"]
    real_dev = U._score_candidate(
        _step5_candidate(title="Senior Java Developer", headline="Senior Java Developer"),
        JAVA_DEV_RUBRIC,
    )["score"]
    assert with_title < no_title, "title mismatch must cost points, not be skipped"
    assert with_title < real_dev, "a QA engineer must not tie a Java developer"


# ---------------------------------------------------------------------------
# The payload has to carry what the pipeline reads
# ---------------------------------------------------------------------------
def test_scoring_payload_carries_the_source():
    """`source` drives the JobAgent location exemption and the N/A stamp."""
    row = _cross_sub_row(source="JobDiva-JobAgent")
    payload = cs.build_scoring_payload(row, json.loads(row["data"]))
    assert payload["source"] == "JobDiva-JobAgent"
    assert _build_candidate_for_resume_matching(payload)["source"] == "JobDiva-JobAgent"


def test_scoring_payload_passes_location_provenance_through():
    """Résumé-is-final: the gate needs to know the résumé overrode the profile."""
    cand = _build_candidate_for_resume_matching({
        "candidate_id": "x",
        "location": "Dallas, TX",
        "data": {
            "location_source": "resume",
            "location_conflict": {"resume": "India", "profile": "Dallas, TX"},
        },
    })
    assert cand["location_source"] == "resume"
    assert cand["location_conflict"]["resume"] == "India"


# ---------------------------------------------------------------------------
# N/A is a verdict, not a zero
# ---------------------------------------------------------------------------
def test_jobagent_row_scores_na_not_a_percentage():
    """Step 5 withholds the % for JobAgent rows; so must every other path."""
    result = _score_via_cross_submissions(_cross_sub_row(source="JobDiva-JobAgent"))
    assert result["score"] is None


def test_na_scores_are_left_off_the_list_rather_than_scored_zero():
    """An unscorable person must not be emailed on a made-up number.

    `select_candidates` used to do `float(result.get("score") or 0)`, which
    turned every N/A verdict into a 0 — harmless for the floor, but it also
    meant the row carried a 0% into the stored list and the email.
    """
    rows = [_cross_sub_row()]
    selected = cs.select_candidates(
        rows,
        new_job_base_refs={"26-22222"},
        existing_person_keys=set(),
        cutoff=NOW - timedelta(days=60),
        scorer=lambda payload, criteria: {"score": None},
        criteria=JAVA_DEV_RUBRIC,
    )
    assert selected == []


def test_no_contact_company_is_skipped_with_its_reason():
    """Kaiser / Citibank / Intuit are never contacted — including from here."""
    rows = [_cross_sub_row()]
    selected = cs.select_candidates(
        rows,
        new_job_base_refs={"26-22222"},
        existing_person_keys=set(),
        cutoff=NOW - timedelta(days=60),
        scorer=lambda payload, criteria: {
            "score": 95,
            "no_contact": True,
            "no_contact_reason": "Current employer 'Kaiser' is on the no-contact list",
        },
        criteria=JAVA_DEV_RUBRIC,
    )
    assert selected == [], "a no-contact row must never reach the recruiter's email"


def test_client_employee_is_skipped():
    rows = [_cross_sub_row()]
    selected = cs.select_candidates(
        rows,
        new_job_base_refs={"26-22222"},
        existing_person_keys=set(),
        cutoff=NOW - timedelta(days=60),
        scorer=lambda payload, criteria: {
            "score": 95,
            "client_conflict": True,
            "client_conflict_reason": "Currently employed by the hiring client",
        },
        criteria=JAVA_DEV_RUBRIC,
    )
    assert selected == []


def test_a_scoring_crash_still_fails_closed_at_the_floor():
    """One bad row must not sink the list, and must not be surfaced either."""
    def boom(payload, criteria):
        raise ValueError("bad row")

    selected = cs.select_candidates(
        [_cross_sub_row()],
        new_job_base_refs={"26-22222"},
        existing_person_keys=set(),
        cutoff=NOW - timedelta(days=60),
        scorer=boom,
        criteria=JAVA_DEV_RUBRIC,
    )
    assert selected == []


# ---------------------------------------------------------------------------
# Criteria: the hard filters have to get their inputs
# ---------------------------------------------------------------------------
def _mock_criteria_conn(sourcing_filters):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (
        [],                      # resume_match_filters
        sourcing_filters,        # sourcing_filters
        "26-99999",              # jobdiva_id
        "Acme",                  # customer_name
        "Onsite",                # location_type
        "Austin",                # city
    )
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


def test_criteria_builder_carries_the_hard_filter_inputs():
    """`min_experience_years` and the radius are pass/fail inputs.

    Left at their defaults (None / 25 mi) the re-score and rescan paths grade
    on a different rubric than the screen: the minimum-years filter reads
    "n/a" and the location gate uses the wrong radius.
    """
    filters = {
        "titles": [{"value": "Java Developer", "matchType": "must"}],
        "skills": [{"value": "Java", "matchType": "must"}],
        "locations": [{"value": "Austin, TX"}],
        "minExperienceYears": 8,
        "sourceLocationMiles": 75,
    }
    with patch("routers.candidates.get_db_connection", return_value=_mock_criteria_conn(filters)):
        criteria = _build_resume_matching_criteria("26-99999")
    assert criteria is not None
    assert criteria.min_experience_years == 8
    assert criteria.within_miles == 75


def test_criteria_builder_reads_a_legacy_per_location_radius():
    """Older drafts stored the radius as "within 50 mi" on the location."""
    filters = {
        "titles": [],
        "skills": [],
        "locations": [{"value": "Austin, TX", "radius": "within 50 mi"}],
    }
    with patch("routers.candidates.get_db_connection", return_value=_mock_criteria_conn(filters)):
        criteria = _build_resume_matching_criteria("26-99999")
    assert criteria.within_miles == 50


def test_criteria_builder_ignores_a_zero_or_junk_minimum():
    """0 / null / "" mean "no floor", not a floor of zero."""
    for raw in (0, None, "", "lots"):
        filters = {"titles": [], "skills": [], "locations": [], "minExperienceYears": raw}
        with patch(
            "routers.candidates.get_db_connection",
            return_value=_mock_criteria_conn(filters),
        ):
            criteria = _build_resume_matching_criteria("26-99999")
        assert criteria.min_experience_years is None, raw


# ---------------------------------------------------------------------------
# Role family must not leak between a search and an off-search scorer
# ---------------------------------------------------------------------------
def test_off_search_scoring_resolves_its_own_role_family():
    """`_current_family` is instance state on a module-level singleton.

    An off-search scorer that read it would inherit whatever family the last
    search left behind — and clobbering it would corrupt a search still
    streaming. The off-search path resolves its own and restores it.
    """
    U._current_family = "healthcare"
    try:
        U.score_candidate_off_search(_step5_candidate(), JAVA_DEV_RUBRIC)
        # Untouched by the off-search pass.
        assert U._current_family == "healthcare"
    finally:
        U._current_family = None


def test_off_search_family_is_derived_from_the_criteria():
    U._current_family = "healthcare"
    try:
        seen = {}

        original = U._collect_scoring_dimensions

        def spy(criteria):
            seen["family"] = U._active_family
            return original(criteria)

        with patch.object(U, "_collect_scoring_dimensions", spy):
            U.score_candidate_off_search(_step5_candidate(), JAVA_DEV_RUBRIC)
        # "Java Developer" is an IT title — NOT the leaked healthcare family.
        assert seen["family"] != "healthcare"
    finally:
        U._current_family = None


# ---------------------------------------------------------------------------
# JobAgent rows: same matrix, but a comparable number
# ---------------------------------------------------------------------------
def test_comparable_criteria_asks_every_source_for_a_percentage():
    """The list is gated and ranked on the %, so every row needs one.

    Without this, Step 5's source-based suppression would drop every
    JobAgent-sourced person out of cross submissions entirely.
    """
    out = cs.comparable_scoring_criteria(JAVA_DEV_RUBRIC)
    assert out.assess_all_sources is True


def test_comparable_criteria_does_not_mutate_the_live_search_criteria():
    """The auto-scan is handed the running search's own criteria object."""
    assert JAVA_DEV_RUBRIC.assess_all_sources is False
    out = cs.comparable_scoring_criteria(JAVA_DEV_RUBRIC)
    assert out is not JAVA_DEV_RUBRIC
    assert JAVA_DEV_RUBRIC.assess_all_sources is False, "clobbered the live search"


def test_comparable_criteria_preserves_the_rest_of_the_rubric():
    out = cs.comparable_scoring_criteria(JAVA_DEV_RUBRIC)
    assert out.skill_criteria == JAVA_DEV_RUBRIC.skill_criteria
    assert out.title_criteria == JAVA_DEV_RUBRIC.title_criteria
    assert out.location_type == JAVA_DEV_RUBRIC.location_type


def test_comparable_criteria_tolerates_a_non_model_and_none():
    """`criteria` is deliberately loose here; a warm/score pass must not crash."""
    sentinel = object()
    assert cs.comparable_scoring_criteria(sentinel) is sentinel
    assert cs.comparable_scoring_criteria(None) is None


def test_jobagent_row_scores_a_real_percentage_under_comparable_criteria():
    """Same matrix as Step 5, minus the source-based % suppression."""
    row = _cross_sub_row(source="JobDiva-JobAgent")
    comparable = cs.comparable_scoring_criteria(JAVA_DEV_RUBRIC)
    result = _score_via_cross_submissions(row, comparable)
    assert result["score"] is not None
    # And it is the SAME number a non-agent row with this profile earns.
    assert result["score"] == _score_via_cross_submissions(_cross_sub_row())["score"]


# ---------------------------------------------------------------------------
# The warm-up bridge: an async warmer, called from the scan's worker thread
# ---------------------------------------------------------------------------
_JOB = {
    "job_id": "2002", "jobdiva_id": "26-22222", "title": "Java Developer",
    "enhanced_title": None, "customer_name": "Globex",
    "recruiter_emails": json.dumps(["rec@pyramidci.com"]),
    "parent_job_id": None, "cross_submissions_checked_at": None,
}


def _mock_scan_conn(prior):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [_JOB, {"job_id": _JOB["job_id"]}, {"id": 1}] + [None] * 50
    cur.fetchall.side_effect = [[], list(prior)]
    return conn


@pytest.mark.asyncio
async def test_async_warmer_runs_on_the_event_loop_before_scoring():
    """`run_for_job` is sync in a worker thread; the warmer is a coroutine.

    Pins the `run_coroutine_threadsafe` bridge: the warmer must actually be
    awaited on the calling loop, with every payload, BEFORE any row is scored.
    """
    import asyncio as aio

    calls = {"warmed": None, "warm_loop": None, "warm_criteria": None,
             "scored_after_warm": []}
    main_loop = aio.get_running_loop()

    async def warmer(payloads, criteria):
        calls["warmed"] = [p["candidate_id"] for p in payloads]
        calls["warm_loop"] = aio.get_running_loop()
        calls["warm_criteria"] = criteria

    def scorer(payload, criteria):
        calls["scored_after_warm"].append(calls["warmed"] is not None)
        return {"score": 90}

    conn = _mock_scan_conn([_cross_sub_row()])
    with patch.object(cs, "get_db_connection", return_value=conn), \
         patch("core.email.notify_cross_submissions", return_value=True):
        summary = await cs.run_for_job_async(
            "26-22222", JAVA_DEV_RUBRIC, scorer, warmer=warmer, force=True,
        )

    assert summary["ran"] is True and summary["selected"] == 1
    assert calls["warmed"] == ["q1"]
    assert calls["warm_loop"] is main_loop, "warmer must run on the caller's loop"
    assert calls["scored_after_warm"] == [True], "scored before the cache was warm"
    # Warmed against the same criteria the rows are scored with.
    assert calls["warm_criteria"].assess_all_sources is True


@pytest.mark.asyncio
async def test_a_failing_warmer_does_not_sink_the_scan():
    """A cold cache costs score fidelity; it must not lose the whole list."""
    import asyncio as aio

    async def warmer(payloads, criteria):
        raise RuntimeError("embedding provider down")

    conn = _mock_scan_conn([_cross_sub_row()])
    with patch.object(cs, "get_db_connection", return_value=conn), \
         patch("core.email.notify_cross_submissions", return_value=True):
        summary = await cs.run_for_job_async(
            "26-22222", JAVA_DEV_RUBRIC, lambda p, c: {"score": 90},
            warmer=warmer, force=True,
        )
    assert summary["ran"] is True and summary["selected"] == 1


@pytest.mark.asyncio
async def test_scan_without_a_warmer_still_runs():
    conn = _mock_scan_conn([_cross_sub_row()])
    with patch.object(cs, "get_db_connection", return_value=conn), \
         patch("core.email.notify_cross_submissions", return_value=True):
        summary = await cs.run_for_job_async(
            "26-22222", JAVA_DEV_RUBRIC, lambda p, c: {"score": 90}, force=True,
        )
    assert summary["ran"] is True and summary["selected"] == 1
