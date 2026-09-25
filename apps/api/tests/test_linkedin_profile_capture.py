"""Step-5 sourcing keeps the WHOLE LinkedIn profile for the JobDiva résumé.

The scoring extraction (`_extract_linkedin_profile_data`) keeps only what the
scorer reads -- no role descriptions, summary, languages. Launch PAIR renders a
JobDiva profile from what the row carries, so the Unipile row now also carries
`linkedin_profile` (services/profile_resume.py `normalize_linkedin_profile`),
and contact details LinkedIn itself shares are used before any paid lookup.
"""
import asyncio

import services.sourced_candidates_storage as storage
import services.unified_candidate_search as ucs
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch

RAW_PROFILE = {
    "first_name": "Ada", "last_name": "Lovelace", "headline": "Principal Data Engineer",
    "summary": "Builds data platforms for trading desks.",
    "location": "Jersey City, New Jersey, United States",
    "public_identifier": "ada-lovelace",
    "work_experience": [{"position": "Principal Data Engineer", "company": "Acme Bank", "start": "1/2021",
                         "description": "Led the lakehouse migration."}],
    "education": [{"school": "Stevens Institute of Technology", "degree": "MS"}],
    "skills": [{"name": "Python"}, {"name": "Spark"}],
    "languages": [{"name": "English", "proficiency": "Native"}],
}


class _FakeUnipile:
    def __init__(self, profile):
        self.profile = profile

    async def get_candidate_profile(self, provider_id, account_id=None):
        return self.profile


def _run_linkedin_search(monkeypatch, profile):
    svc = UnifiedCandidateSearch()
    search_row = {
        "id": "unipile_AEMAA1", "provider_id": "AEMAA1", "name": "Ada Lovelace",
        "title": "Principal Data Engineer | Spark", "source": "LinkedIn-Unipile",
        "profile_url": "https://www.linkedin.com/in/ada-lovelace", "location": "Jersey City, NJ", "email": "",
    }

    async def _search_linkedin(criteria):
        return {"candidates": [search_row], "source_type": "LinkedIn-Unipile"}

    async def _llm_extraction(cand):  # no LLM in tests
        return {"raw": {"candidate_name": "Ada Lovelace", "job_title": "Principal Data Engineer"}}

    def _policy(cand, criteria):
        cand["match_score"] = 90
        return cand

    paid_lookups = []

    async def _paid_chain(cand, criteria, *, overwrite):
        paid_lookups.append(cand.get("id"))

    svc._search_linkedin = _search_linkedin
    svc.unipile_service = _FakeUnipile(profile)
    svc._candidate_title_match = lambda cand, criteria: True
    svc._candidate_below_min_years_pre_llm = lambda cand, criteria: False
    svc._filter_assessment = lambda cand, criteria, enforce_years=False: {
        "passes": True, "matched": [], "missing": [], "excluded": [], "score": 0,
    }
    svc.apply_scoring_policy = _policy
    svc._apply_contact_enrichment = _paid_chain
    monkeypatch.setattr(storage, "process_linkedin_candidate", _llm_extraction)
    monkeypatch.setattr(ucs, "apply_no_contact_flag", lambda cand: False)

    async def _collect():
        criteria = SearchCriteria(job_id="job-li", sources=["LinkedIn"])
        return [ev async for ev in svc.search_candidates(criteria)]

    events = asyncio.run(_collect())
    shown = [ev["data"] for ev in events if ev.get("type") == "candidate"]
    return shown, paid_lookups


def test_unipile_row_carries_the_full_profile(monkeypatch):
    shown, _paid = _run_linkedin_search(monkeypatch, RAW_PROFILE)

    (row,) = shown
    profile = row["linkedin_profile"]
    assert profile["summary"] == "Builds data platforms for trading desks."
    assert profile["experience"][0]["description"] == "Led the lakehouse migration."
    assert profile["languages"] == [{"name": "English", "proficiency": "Native"}]
    assert profile["public_profile_url"] == "https://www.linkedin.com/in/ada-lovelace"


def test_contact_linkedin_shares_is_used_before_any_paid_lookup(monkeypatch):
    shown, paid = _run_linkedin_search(monkeypatch, {
        **RAW_PROFILE,
        "contact_info": {"emails": ["ada@acme-bank.dev"], "phones": ["+1 (201) 555-0100"]},
    })

    (row,) = shown
    assert row["email"] == "ada@acme-bank.dev"
    assert row["phone"] == "+12015550100"
    assert paid == []  # already reachable: no ZoomInfo / Apollo / Exa spend


def test_without_linkedin_contact_the_paid_chain_still_runs(monkeypatch):
    shown, paid = _run_linkedin_search(monkeypatch, RAW_PROFILE)

    assert shown[0]["email"] == ""
    assert paid == ["unipile_AEMAA1"]
