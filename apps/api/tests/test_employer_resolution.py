"""Launch-time employer resolution (services/employer_resolution.py).

Policy under test (2026-09-02): whenever a candidate's employer data is
missing or not confident, fetch the resume and parse it before PAIR outreach;
JobDiva's CandidatesProfileDetail work history corroborates; candidates that
still resolve to nothing launch but are reported employer-unverified.
"""

import asyncio
from typing import List

from services.company_match import client_appears_in_text
from services.employer_resolution import (
    candidate_profile_texts,
    employer_verification_state,
    has_confident_employer_signal,
    parse_profile_experience,
    profile_current_and_last_texts,
    resolve_employer_signals,
)
from routers.engagement import (
    _merge_employer_signals,
    is_candidate_excluded_from_pair,
)


# ── JobDiva profile EXPERIENCE parsing ─────────────────────────────────────

def test_parse_profile_experience_closed_and_open_ranges():
    record = {"EXPERIENCE": [
        {"DATE": "08/2023 - 11/2024", "DETAILS": "Data Engineer | Walmart, Atlanta, USA"},
        {"DATE": "01/2020 - present", "DETAILS": "The Mutual Group - Des Moines, IA"},
        {"DATE": "01/2019 - ", "DETAILS": "ESS Inc"},
        {"DATE": "", "DETAILS": "Software Developer"},
        {"DATE": "03/2021 - 04/2022", "DETAILS": ""},  # no text → dropped
        "not-a-dict",
    ]}
    entries = parse_profile_experience(record)
    assert [e["text"] for e in entries] == [
        "Data Engineer | Walmart, Atlanta, USA",
        "The Mutual Group - Des Moines, IA",
        "ESS Inc",
        "Software Developer",
    ]
    assert entries[0]["end"] == (2024, 11)
    assert entries[0]["is_current"] is False
    # "present" and a dangling "- " both mean an open, current engagement
    assert entries[1]["is_current"] is True
    assert entries[2]["is_current"] is True
    # no DATE at all → we can't claim it's current
    assert entries[3]["is_current"] is False


def test_parse_profile_experience_tolerates_missing_record():
    assert parse_profile_experience(None) == []
    assert parse_profile_experience({}) == []
    assert parse_profile_experience({"EXPERIENCE": "garbage"}) == []


def test_profile_current_and_last_split_picks_most_recent_past():
    entries = parse_profile_experience({"EXPERIENCE": [
        {"DATE": "01/2008 - 01/2019", "DETAILS": "old fragment"},
        {"DATE": "01/2026 - 07/2026", "DETAILS": "The Mutual Group - Des Moines, IA"},
        {"DATE": "12/2022 - 12/2025", "DETAILS": "NYC Department of Education"},
    ]})
    current, last = profile_current_and_last_texts(entries)
    assert current == []
    # 07/2026 beats 12/2025 beats 01/2019 — only the single most recent counts
    assert last == ["The Mutual Group - Des Moines, IA"]


def test_profile_last_falls_back_to_list_order_when_undated():
    current, last = profile_current_and_last_texts([
        {"text": "first listed", "start": None, "end": None, "is_current": False},
        {"text": "second listed", "start": None, "end": None, "is_current": False},
    ])
    assert last == ["first listed"]
    assert current == []


# ── confidence ─────────────────────────────────────────────────────────────

def test_company_experience_is_confident():
    assert has_confident_employer_signal(
        {"company_experience": [{"company": "Stripe", "end_date": "Present"}]}
    )
    assert has_confident_employer_signal(
        {"data": {"enhanced_info": {"company_experience": [{"employer": "Stripe"}]}}}
    )


def test_headline_or_empty_history_is_not_confident():
    # Per policy, anything less than parsed employment history means the
    # resume gets fetched and parsed.
    assert not has_confident_employer_signal({"headline": "Engineer at Stripe"})
    assert not has_confident_employer_signal({"company_experience": []})
    assert not has_confident_employer_signal({"company_experience": [{"title": "Engineer"}]})
    assert not has_confident_employer_signal({})


def test_flat_current_company_counts_as_confident():
    # A future recruiter override lands in current_company and must win.
    assert has_confident_employer_signal({"current_company": "Stripe"})


# ── one-directional text matching ──────────────────────────────────────────

def test_client_in_noisy_text_matches():
    assert client_appears_in_text(
        "SAS Programmer | WELLS FARGO, WEST DES MOINES, IOWA", "Wells Fargo"
    )
    assert client_appears_in_text(
        "Data Engineer | Walmart, Atlanta, USA", "Walmart Inc."
    )


def test_fragment_never_matches_a_client_that_merely_contains_it():
    # The reverse direction of is_same_company is deliberately absent here:
    # profile lines are sentences, not company names.
    assert not client_appears_in_text("India", "India Tech Solutions")
    assert not client_appears_in_text("Software Developer", "Developer Bank")


def test_placeholder_client_never_matches_text():
    assert not client_appears_in_text("works at unknown customer", "Unknown Customer")
    assert not client_appears_in_text("anything", "")


# ── the resolution pass ────────────────────────────────────────────────────

class _FakeJobDiva:
    def __init__(self, profiles=None, resumes=None):
        self.profiles = profiles or {}
        self.resumes = resumes or {}
        self.profile_calls: List[List[str]] = []
        self.resume_calls: List[List[str]] = []

    async def fetch_candidate_profiles_batch(self, ids):
        self.profile_calls.append(list(ids))
        return {cid: rec for cid, rec in self.profiles.items() if cid in ids}

    async def fetch_resume_texts(self, ids):
        self.resume_calls.append(list(ids))
        return {cid: text for cid, text in self.resumes.items() if cid in ids}


REAL_RESUME = (
    "JANE DOE\nEXPERIENCE\nSenior Engineer, Acme Corp, Jan 2020 - Present\n"
    "Built things at scale for a long time across many projects and teams."
)


def test_resolution_skips_confident_and_non_jobdiva_rows(monkeypatch):
    service = _FakeJobDiva()
    called: List[str] = []

    async def fake_process(payload):
        called.append(payload["candidate_id"])
        return {"company_experience": [{"company": "Acme"}], "raw": {}}

    import services.sourced_candidates_storage as storage
    monkeypatch.setattr(storage, "process_jobdiva_candidate", fake_process)

    out = asyncio.run(resolve_employer_signals(
        [
            {"candidate_id": "111", "source": "JobDiva-TalentSearch",
             "company_experience": [{"company": "Stripe", "end_date": "Present"}]},
            {"candidate_id": "AEMAAxyz", "source": "LinkedIn-Unipile"},
        ],
        service=service,
    ))
    assert out == {}
    assert called == []
    assert service.profile_calls == []


def test_resolution_parses_stored_resume_without_refetch(monkeypatch):
    service = _FakeJobDiva()

    async def fake_process(payload):
        assert payload["resume_text"] == REAL_RESUME
        return {
            "candidate_id": payload["candidate_id"],
            "company_experience": [{"company": "Acme Corp", "end_date": "Present"}],
            "current_title": "Senior Engineer",
            "raw": {},
        }

    import services.sourced_candidates_storage as storage
    monkeypatch.setattr(storage, "process_jobdiva_candidate", fake_process)

    out = asyncio.run(resolve_employer_signals(
        [{"candidate_id": "222", "source": "JobDiva-Applicants", "resume_text": REAL_RESUME}],
        service=service,
    ))
    signals = out["222"]
    assert signals["company_experience"] == [{"company": "Acme Corp", "end_date": "Present"}]
    assert signals["title"] == "Senior Engineer"
    assert signals["employer_resolution"]["extraction"] == "completed"
    # stored resume was real → no JobDiva resume fetch
    assert service.resume_calls == []


def test_resolution_fetches_missing_resume_and_attaches_profile(monkeypatch):
    service = _FakeJobDiva(
        profiles={"333": {"EXPERIENCE": [
            {"DATE": "01/2024 - ", "DETAILS": "Data Engineer | Walmart, Atlanta"},
        ]}},
        resumes={"333": REAL_RESUME},
    )

    async def fake_process(payload):
        return {
            "company_experience": [{"company": "Acme Corp", "end_date": "Present"}],
            "raw": {},
        }

    import services.sourced_candidates_storage as storage
    monkeypatch.setattr(storage, "process_jobdiva_candidate", fake_process)

    out = asyncio.run(resolve_employer_signals(
        [{"candidate_id": "333", "source": "JobDiva-JobAgent"}],
        service=service,
    ))
    assert service.resume_calls == [["333"]]
    assert out["333"]["company_experience"]
    entries = out["333"]["jobdiva_profile_experience"]
    assert entries and entries[0]["text"] == "Data Engineer | Walmart, Atlanta"
    assert entries[0]["is_current"] is True


def test_resolution_reports_no_resume_but_keeps_profile(monkeypatch):
    service = _FakeJobDiva(
        profiles={"444": {"EXPERIENCE": [
            {"DATE": "01/2020 - 02/2021", "DETAILS": "ESS Inc"},
        ]}},
    )

    async def fake_process(payload):  # pragma: no cover — must not be called
        raise AssertionError("no resume text → extraction must not run")

    import services.sourced_candidates_storage as storage
    monkeypatch.setattr(storage, "process_jobdiva_candidate", fake_process)

    out = asyncio.run(resolve_employer_signals(
        [{"candidate_id": "444", "source": "JobDiva-TalentSearch",
          "resume_text": "Available upon request"}],
        service=service,
    ))
    meta = out["444"]["employer_resolution"]
    assert meta["extraction"] == "no_resume"
    assert meta["profile_entries"] == 1
    assert "company_experience" not in out["444"]


def test_resolution_survives_service_failures(monkeypatch):
    class _Exploding:
        async def fetch_candidate_profiles_batch(self, ids):
            raise RuntimeError("boom")

        async def fetch_resume_texts(self, ids):
            raise RuntimeError("boom")

    out = asyncio.run(resolve_employer_signals(
        [{"candidate_id": "555", "source": "JobDiva-TalentSearch"}],
        service=_Exploding(),
    ))
    assert out["555"]["employer_resolution"]["extraction"] == "no_resume"


def test_resolution_disabled_by_kill_switch(monkeypatch):
    from core import sourcing_config
    monkeypatch.setattr(sourcing_config, "EMPLOYER_RESOLUTION_ENABLED", False)
    out = asyncio.run(resolve_employer_signals(
        [{"candidate_id": "666", "source": "JobDiva-TalentSearch"}],
        service=_FakeJobDiva(),
    ))
    assert out == {}


# ── merge + gate integration ───────────────────────────────────────────────

def test_merge_carries_profile_experience_fill_if_absent():
    merged = _merge_employer_signals(
        {"candidate_id": "1"},
        signals={"jobdiva_profile_experience": [{"text": "ESS Inc", "is_current": True}]},
    )
    assert merged["jobdiva_profile_experience"] == [{"text": "ESS Inc", "is_current": True}]
    kept = _merge_employer_signals(
        {"jobdiva_profile_experience": [{"text": "already here"}]},
        signals={"jobdiva_profile_experience": [{"text": "newer"}]},
    )
    assert kept["jobdiva_profile_experience"] == [{"text": "already here"}]


def test_gate_excludes_on_current_profile_line_naming_the_client():
    candidate = {
        "candidate_id": "9",
        "jobdiva_profile_experience": [
            {"text": "SAS Programmer | WELLS FARGO, WEST DES MOINES", "start": None,
             "end": None, "is_current": True},
        ],
    }
    excluded, reason = is_candidate_excluded_from_pair(candidate, "Wells Fargo")
    assert excluded
    assert reason == "Employed by Hiring Client (JobDiva profile)"


def test_gate_uses_profile_last_line_only_when_nothing_else_exists():
    candidate = {
        "candidate_id": "9",
        "jobdiva_profile_experience": [
            {"text": "Analyst | Wells Fargo, Charlotte", "start": None,
             "end": (2026, 1), "is_current": False},
        ],
    }
    excluded, reason = is_candidate_excluded_from_pair(candidate, "Wells Fargo")
    assert excluded
    assert reason == "Employed by Hiring Client (last known employer, JobDiva profile)"

    # A structured, non-client last employer already cleared the ladder — the
    # noisier profile line must not second-guess it.
    candidate_with_structured_last = dict(candidate)
    candidate_with_structured_last["company_experience"] = [
        {"company": "Stripe", "end_date": "2026-05"},
    ]
    excluded, _ = is_candidate_excluded_from_pair(candidate_with_structured_last, "Wells Fargo")
    assert not excluded


def test_gate_structured_current_employer_beats_profile_lines():
    candidate = {
        "candidate_id": "9",
        "company_experience": [{"company": "Stripe", "end_date": "Present"}],
        "jobdiva_profile_experience": [
            {"text": "Wells Fargo", "start": None, "end": None, "is_current": True},
        ],
    }
    excluded, _ = is_candidate_excluded_from_pair(candidate, "Wells Fargo")
    assert not excluded


def test_gate_flags_no_contact_from_profile_lines(monkeypatch):
    candidate = {
        "candidate_id": "9",
        "jobdiva_profile_experience": [
            {"text": "RN Case Manager - Kaiser Permanente, Oakland CA",
             "start": None, "end": (2026, 3), "is_current": False},
        ],
    }
    excluded, reason = is_candidate_excluded_from_pair(candidate, "")
    assert excluded
    assert reason == "No-Contact Company (Kaiser)"


def test_gate_profile_fragment_does_not_exclude_and_counts_as_judged():
    candidate = {
        "candidate_id": "9",
        "jobdiva_profile_experience": [
            {"text": "India", "start": None, "end": (2024, 11), "is_current": False},
        ],
    }
    excluded, reason = is_candidate_excluded_from_pair(candidate, "India Tech Solutions")
    assert not excluded
    assert reason == ""


# ── verification state ─────────────────────────────────────────────────────

def test_verification_state_ladder():
    assert employer_verification_state(
        {"company_experience": [{"company": "Stripe", "end_date": "Present"}]}
    ) == "verified"
    assert employer_verification_state(
        {"jobdiva_profile_experience": [
            {"text": "ESS Inc", "start": None, "end": (2026, 5), "is_current": False},
        ]}
    ) == "profile_only"
    assert employer_verification_state({"candidate_id": "1"}) == "unverified"


def test_candidate_profile_texts_reads_nested_data_blob():
    current, last = candidate_profile_texts({
        "data": {"jobdiva_profile_experience": [
            {"text": "Acme", "start": None, "end": None, "is_current": True},
        ]},
    })
    assert current == ["Acme"]
    assert last == []
