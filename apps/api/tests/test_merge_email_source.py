"""Where a merged row's email came from (``email_source``, 2026-10-15).

Launch PAIR seeds its contact lookup with the email a JobDiva row holds
(launch-rules.ts getCandidateSeedEmail / rules.get_candidate_seed_email) and
then keeps that email over any the lookup finds. A JobDiva row with no (or an
Auto_*) email takes a LinkedIn / Exa / Dice row's email when the search's
cross-source dedup merges the two (_merge_candidate_best_of), so the merge
stamps ``email_source`` ("jobdiva" | "other") and the streamed patches carry
it; otherwise the extracted email went out as the JobDiva-held seed and a
found address was thrown away. The browser's own merge
(deduplicateCandidatesUI) does the same: launch_rules fixtures.

These tests pin:
  - a JobDiva survivor adopting another source's email stamps "other"; one
    adopting a JobDiva row's email stamps "jobdiva"; a stamp carries through
    a later merge; an email the survivor keeps is left alone
  - a non-JobDiva survivor the merge turns into a JobDiva row keeps its own
    email as "other"
  - the take-over stream (a JobDiva row absorbing an already-shown Exa row)
    sends email_source in the scored patch, so the browser never seeds it
"""
import asyncio

from core import sourcing_config
from services import contact_enrichment as ce
from services.launch_sessions import rules
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch

from tests.test_contact_enrichment_apollo_first import _deep_entry, _FakeExaAgent
from tests.test_jobagent_quick_first import _patch_service_for_orchestration

LINKEDIN = "https://www.linkedin.com/in/jane-doe"


def _merge(dst, src):
    return UnifiedCandidateSearch()._merge_candidate_best_of(dst, src)


def test_a_jobdiva_survivor_taking_another_sources_email_marks_it_other():
    jd = {"candidate_id": "jd", "source": "JobDiva-TalentSearch", "email": "Auto_7@jobdiva.com"}
    li = {"candidate_id": "li", "source": "LinkedIn-Exa", "email": "jane.extracted@acme.com"}

    changed = _merge(jd, li)

    assert jd["email"] == "jane.extracted@acme.com" and jd["email_source"] == "other"
    assert changed["email"] == "jane.extracted@acme.com" and changed["email_source"] == "other"
    # What the browser does with the row: no seed, so the lookup buys the
    # email and its answer replaces the extracted one.
    assert rules.get_candidate_seed_email(jd) == ""


def test_an_emailless_jobdiva_survivor_taking_another_sources_email_marks_it_other():
    jd = {"candidate_id": "jd", "source": "JobDiva-Applicants", "jobdiva_candidate_id": "77"}
    dice = {"candidate_id": "d1", "source": "Dice", "email": "jane@acme.com"}

    changed = _merge(jd, dice)

    assert changed["email_source"] == "other" and rules.get_candidate_seed_email(jd) == ""


def test_a_survivor_taking_a_jobdiva_rows_email_marks_it_jobdiva():
    owner = {"candidate_id": "jd-a", "source": "JobDiva-JobAgent", "email": "Auto_1@jobdiva.com"}
    dup = {"candidate_id": "jd-t", "source": "JobDiva-TalentSearch", "email": "jane@acme.com"}

    changed = _merge(owner, dup)

    assert changed["email_source"] == "jobdiva"
    assert rules.get_candidate_seed_email(owner) == "jane@acme.com"


def test_the_stamp_carries_through_a_later_merge():
    first = {"candidate_id": "jd-t", "source": "JobDiva-TalentSearch", "email": ""}
    _merge(first, {"candidate_id": "li", "source": "LinkedIn", "email": "jane.extracted@acme.com"})
    survivor = {"candidate_id": "jd-a", "source": "JobDiva-JobAgent", "email": "Auto_1@jobdiva.com"}

    changed = _merge(survivor, first)   # a JobDiva row, but its email is LinkedIn's

    assert changed["email_source"] == "other" and rules.get_candidate_seed_email(survivor) == ""


def test_an_email_the_survivor_keeps_is_not_restamped():
    jd = {"candidate_id": "jd", "source": "JobDiva-TalentSearch", "email": "jane@acme.com"}

    changed = _merge(jd, {"candidate_id": "li", "source": "LinkedIn-Exa", "email": "jane.other@acme.com"})

    assert "email_source" not in jd and "email_source" not in changed
    assert rules.get_candidate_seed_email(jd) == "jane@acme.com"


def test_a_non_jobdiva_survivor_made_jobdiva_keeps_its_email_as_other():
    """The other direction: an Exa row absorbs a JobDiva duplicate whose email
    it does not take. Its sources / JobDiva id now read as JobDiva, but its
    email is still the extracted one."""
    li = {"candidate_id": "li", "source": "LinkedIn-Exa", "email": "jane.extracted@acme.com"}
    jd = {"candidate_id": "jd", "source": "JobDiva-Applicants", "jobdiva_candidate_id": "77",
          "email": "Auto_77@jobdiva.com"}

    changed = _merge(li, jd)

    assert li["sources"] == ["LinkedIn-Exa", "JobDiva-Applicants"]
    assert changed["email_source"] == "other" and li["email"] == "jane.extracted@acme.com"
    assert rules.get_candidate_seed_email(li) == ""


def test_a_non_jobdiva_survivor_taking_a_jobdiva_email_marks_it_jobdiva():
    li = {"candidate_id": "li", "source": "LinkedIn-Exa", "email": ""}
    jd = {"candidate_id": "jd", "source": "JobDiva-Applicants", "email": "jane@acme.com"}

    changed = _merge(li, jd)

    assert changed["email_source"] == "jobdiva" and rules.get_candidate_seed_email(li) == "jane@acme.com"


def test_a_merge_between_two_non_jobdiva_rows_stamps_nothing_it_keeps():
    li = {"candidate_id": "li", "source": "LinkedIn-Exa", "email": "jane@acme.com"}

    changed = _merge(li, {"candidate_id": "u", "source": "LinkedIn-Unipile", "phone": "+15125550100"})

    assert "email_source" not in changed and "email_source" not in li


def test_a_take_over_streams_email_source_in_the_scored_patch(monkeypatch):
    """An Exa deep-search row is shown and its lookup finds an email; the
    JobDiva-JobAgent row for the same person (Auto_* email) arrives later and
    takes the slot, absorbing that email. The scored patch must say where it
    came from, or the browser seeds it as the JobDiva-held email."""
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", True)
    monkeypatch.setattr(sourcing_config, "EXTERNAL_SOURCE_MIN_SCORE", 60)
    monkeypatch.setattr(sourcing_config, "JOBAGENT_QUICK_FIRST_COUNT", 0)
    exa_row_enriched = asyncio.Event()

    async def _fake_sourcing_chain(linkedin_url, jobdiva_id=None, **kwargs):
        exa_row_enriched.set()
        return {"workEmail": "jane.found@acme.com", "provider_used": "apollo"}

    monkeypatch.setattr(ce, "enrich_contact_for_sourcing", _fake_sourcing_chain)

    svc = UnifiedCandidateSearch()
    _patch_service_for_orchestration(svc, {"calls": [], "full_returned": False})

    async def _jobagent_after_the_exa_row(criteria, resume_count_override=None):
        await asyncio.wait_for(exa_row_enriched.wait(), timeout=10)
        await asyncio.sleep(0.05)   # the contact_enrichment patch lands first
        return {
            "candidates": [{
                "candidate_id": "jd1", "id": "jd1", "name": "Jane Doe", "source": "JobDiva-JobAgent",
                "jobdiva_candidate_id": "jd1", "api_rank": 1, "email": "Auto_1@jobdiva.com",
                "profile_url": LINKEDIN, "resume_text": "Senior data engineer " * 10,
            }],
            "source_type": "JobDiva-JobAgent",
            "jobdiva_criteria_unconfigured": False,
        }

    async def _empty_pass_a(criteria):
        return {"candidates": [], "source_type": "LinkedIn-Exa"}

    def _policy(cand, criteria):
        cand["match_score"] = 90
        return cand

    svc._search_jobdiva_talent = _jobagent_after_the_exa_row
    svc.exa_service = _FakeExaAgent([_deep_entry("Jane Doe", "jane-doe")])
    svc._search_exa = _empty_pass_a
    svc.apply_scoring_policy = _policy
    svc._filter_assessment = lambda cand, criteria, enforce_years=False: {
        "passes": True, "matched": [], "missing": [], "excluded": [], "score": 0,
    }

    async def _run():
        criteria = SearchCriteria(job_id="job-merge", sources=["Exa", "JobDiva-JobAgent"])
        return [ev async for ev in svc.search_candidates(criteria)]

    events = asyncio.run(_run())

    rows = {ev["data"]["candidate_id"]: ev["data"] for ev in events if ev.get("type") == "candidate"}
    exa_id = next(cid for cid, row in rows.items() if row.get("source") != "JobDiva-JobAgent")
    dropped = [ev for ev in events if ev.get("type") == "candidate_detail" and ev.get("stage") == "dropped"]
    assert [ev["candidate_id"] for ev in dropped] == [exa_id]
    [scored] = [ev for ev in events
                if ev.get("type") == "candidate_detail" and ev.get("stage") == "scored"
                and ev["candidate_id"] == "jd1"]
    assert scored["patch"]["email"] == "jane.found@acme.com"
    assert scored["patch"]["email_source"] == "other"

    # Replayed through the browser's stream reducer (the server runner's
    # port), the surviving JobDiva row is not seeded.
    state = rules.initial_stream_state()
    for ev in events:
        state = rules.apply_stream_event(state, ev)
    [row] = [c for c in state["candidates"] if rules.candidate_row_id(c) == "jd1"]
    assert row["email"] == "jane.found@acme.com"
    assert rules.get_candidate_seed_email(row) == ""
