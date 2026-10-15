"""Paid Exa extras are OFF unless an operator turns them on.

Pass B (the Exa Agent deep-search sweep, $0.10 a run) and the per-URL
get_contents() deep analysis both default off (2026-10-14): Pass B runs were
paid for and thrown away on PROD (exa_py 2.13 cannot parse a fractional
agentComputeUnits), and turning Pass B off alone silently switches the
per-URL deep analysis on, so the two must stay off together.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from core import sourcing_config
from services import contact_enrichment as ce
from services import sourced_candidates_storage
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch

API_DIR = Path(__file__).resolve().parents[1]


def _import_time_default(var: str) -> str:
    """Value of sourcing_config.<var> in a fresh interpreter with the env var
    unset and dotenv disabled, so a developer's apps/api/.env cannot leak in."""
    env = {k: v for k, v in os.environ.items() if k != var}
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
        "from core import sourcing_config as s\n"
        f"print(s.{var})\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=API_DIR, env=env,
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip().splitlines()[-1]


def test_pass_b_is_off_by_default():
    assert _import_time_default("EXA_AGENT_ENABLED") == "False"


def test_deep_analysis_is_off_by_default(monkeypatch):
    monkeypatch.delenv("EXA_DEEP_ANALYSIS_ENABLED", raising=False)
    assert sourcing_config.exa_deep_analysis_enabled() is False


def test_deep_analysis_accepts_truthy_variants(monkeypatch):
    for raw in ("1", "true", "YES", "on"):
        monkeypatch.setenv("EXA_DEEP_ANALYSIS_ENABLED", raw)
        assert sourcing_config.exa_deep_analysis_enabled() is True
    monkeypatch.setenv("EXA_DEEP_ANALYSIS_ENABLED", "false")
    assert sourcing_config.exa_deep_analysis_enabled() is False


def _row(name, slug):
    return {
        "id": f"exa-{slug}",
        "candidate_id": f"exa-{slug}",
        "name": name,
        "title": "Data Engineer",
        "profile_url": f"https://www.linkedin.com/in/{slug}",
        "source": "LinkedIn-Exa",
    }


def _full_exa_search(monkeypatch):
    """Run a full-mode Step-5 search over one LinkedIn-Exa row and report
    which paid Exa extras were called."""
    monkeypatch.setattr(sourcing_config, "EXTERNAL_SOURCE_MIN_SCORE", 60)

    async def _no_llm(cand):
        return cand

    monkeypatch.setattr(sourced_candidates_storage, "process_linkedin_candidate", _no_llm)

    async def _no_contact(linkedin_url, jobdiva_id=None, **kwargs):
        return {}

    monkeypatch.setattr(ce, "enrich_contact_for_sourcing", _no_contact)

    svc = UnifiedCandidateSearch()
    paid = []

    async def _deep_research(*args, **kwargs):
        paid.append("deep_research_candidates")
        return []

    async def _deep_analyze(*args, **kwargs):
        paid.append("deep_analyze_candidate")
        return {}

    monkeypatch.setattr(svc.exa_service, "deep_research_candidates", _deep_research)
    monkeypatch.setattr(svc.exa_service, "deep_analyze_candidate", _deep_analyze)

    async def _pass_a(criteria):
        return {"candidates": [_row("Shown Person", "shown")], "source_type": "LinkedIn-Exa"}

    def _policy(cand, criteria):
        cand["match_score"] = 90
        return cand

    svc._search_exa = _pass_a
    svc.apply_scoring_policy = _policy
    svc._candidate_title_match = lambda cand, criteria: True
    svc._candidate_outside_years_range_pre_llm = lambda cand, criteria: False
    svc._filter_assessment = lambda cand, criteria, enforce_years=False: {
        "passes": True, "matched": [], "missing": [], "excluded": [], "score": 0,
    }

    async def _run():
        return [ev async for ev in svc.search_candidates(SearchCriteria(job_id="job-defaults", sources=["Exa"]))]

    events = asyncio.run(_run())
    assert [ev["data"]["name"] for ev in events if ev.get("type") == "candidate"] == ["Shown Person"]
    return paid


def test_full_exa_search_buys_no_paid_extras_by_default(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", _import_time_default("EXA_AGENT_ENABLED") == "True")
    monkeypatch.delenv("EXA_DEEP_ANALYSIS_ENABLED", raising=False)

    assert _full_exa_search(monkeypatch) == []


def test_pass_b_off_alone_still_turns_deep_analysis_on(monkeypatch):
    # Pins the coupling the defaults guard against: with Pass B off, an
    # operator who sets EXA_DEEP_ANALYSIS_ENABLED pays per profile.
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", False)
    monkeypatch.setenv("EXA_DEEP_ANALYSIS_ENABLED", "true")

    assert _full_exa_search(monkeypatch) == ["deep_analyze_candidate"]
