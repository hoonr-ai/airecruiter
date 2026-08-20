"""JobDiva producer selection (2026-08-01).

Step 5 used to have a single "JobDiva Talent" checkbox that always fired
BOTH talent pools — JobDiva's own JobAgent matcher and our generated
boolean TalentSearch — so a recruiter could not spend the search budget on
just the one that works for a given req. The wizard now sends explicit
"JobDiva-JobAgent" / "JobDiva-TalentSearch" source names, one per checkbox.

These pin the source-name contract, in particular the back-compat leg:
saved Step-5 drafts and non-wizard callers still send bare "JobDiva", and
that must keep meaning "run both pools" — the alternative (treating it as
neither) would silently stop sourcing for every existing job.
"""
from services.unified_candidate_search import resolve_jobdiva_sources  # noqa: E402


def test_legacy_jobdiva_runs_both_talent_pools():
    """Saved drafts / non-wizard callers send bare "JobDiva"."""
    sel = resolve_jobdiva_sources(["JobDiva", "LinkedIn"])
    assert sel["jobagent"] is True
    assert sel["talentsearch"] is True
    assert sel["applicants"] is False


def test_jobagent_only():
    sel = resolve_jobdiva_sources(["JobDiva-JobAgent", "LinkedIn"])
    assert sel["jobagent"] is True
    assert sel["talentsearch"] is False


def test_talent_search_only():
    sel = resolve_jobdiva_sources(["JobDiva-TalentSearch", "Exa"])
    assert sel["jobagent"] is False
    assert sel["talentsearch"] is True


def test_both_explicit_pools():
    sel = resolve_jobdiva_sources(["JobDiva-JobAgent", "JobDiva-TalentSearch"])
    assert sel["jobagent"] is True
    assert sel["talentsearch"] is True


def test_no_jobdiva_source_runs_neither_pool():
    sel = resolve_jobdiva_sources(["LinkedIn", "Exa"])
    assert sel == {"applicants": False, "jobagent": False, "talentsearch": False}


def test_applicants_is_independent_of_the_talent_pools():
    """Auto-sync passes "JobDiva Applicants" and must not pull in a talent
    pool — Step-5 sourcing deliberately does not fetch applicants."""
    for name in ("JobDiva Applicants", "JobDiva-Applicants"):
        sel = resolve_jobdiva_sources([name])
        assert sel["applicants"] is True, name
        assert sel["jobagent"] is False, name
        assert sel["talentsearch"] is False, name


def test_empty_and_none_sources_select_nothing():
    for sources in ([], None):
        assert resolve_jobdiva_sources(sources) == {
            "applicants": False,
            "jobagent": False,
            "talentsearch": False,
        }
