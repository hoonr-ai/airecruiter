"""The applicant sync must never relabel a provisioned person as a JobDiva applicant.

Launch PAIR records a JobDiva application for every person it provisions, so
JobDiva's applicant list then includes the Exa/LinkedIn/Dice people too. The
15-minute sync (`jobdiva_applicant_auto_sync` -> `synchronize_job_applicants`)
must match each applicant back to the local row that already exists for that
person and update it in place -- keeping its origin `source` -- and insert only
genuinely new applicants.

Pins:
1. the lookup index is built for the REAL job ids (a dict row was tuple-unpacked
   into the column names, so the index matched nobody and every applicant,
   provisioned people included, became a fresh JobDiva-Applicants row);
2. matching by the provisioner's stamp, the synthetic pair-email, national
   phone digits and normalised LinkedIn URLs;
3. a match is an UPDATE that never assigns `source`, keeps a real Step-5 score
   over the sync's placeholder 0, and is not auto-launched again;
4. applicant provenance: new applicants are "organic"; an applicant JobDiva
   attributes to PAIR with no local row is skipped, not re-imported.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

import services.auto_assign_service as aas
from services.auto_assign_service import (
    AutoAssignService,
    PAIR_APPLICATION_UNLINKED,
    _monitored_job_ids,
)

JOB_REF = "26-1"
JOB_NUM = "123"
EXA_ID = "exa_linkedin.com/in/ada"
ADA_JD = "777"


def _index_row(**over) -> Dict[str, Any]:
    row = {
        "id": 1, "candidate_id": EXA_ID, "source": "LinkedIn-Exa", "email": "ada@work.example.com",
        "phone": "", "name": "Ada Lovelace", "headline": "", "location": "", "profile_url": "",
        "resume_text": "resume", "resume_match_percentage": 84,
        "data": {"jobdiva_candidate_id": ADA_JD, "match_score": 84, "explainability": ["Strong fit"],
                 "jobdiva_application_origin": "pair", "jobdiva_profile_origin": "pair"},
        "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "jcid": ADA_JD, "email_lc": "ada@work.example.com", "data_email_lc": "",
        "phone_norm": "", "data_phone_norm": "", "profile_url_norm": "", "linkedin_norm": "",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# 1. the index is built for the job's real ids
# ---------------------------------------------------------------------------

def test_monitored_job_ids_reads_dict_and_tuple_rows():
    assert _monitored_job_ids({"jobdiva_id": JOB_REF, "job_id": 123}, "x") == (JOB_REF, "123")
    assert _monitored_job_ids((JOB_REF, "123"), "x") == (JOB_REF, "123")
    assert _monitored_job_ids(None, JOB_REF) == (JOB_REF, JOB_REF)
    # blanks fall back to the requested id rather than matching nothing
    assert _monitored_job_ids({"jobdiva_id": None, "job_id": ""}, JOB_REF) == (JOB_REF, JOB_REF)


class _IndexCursor:
    """RealDictCursor stand-in for _build_candidate_lookup_index."""

    def __init__(self, rows):
        self.rows = rows
        self.executed: List[Any] = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return {"jobdiva_id": JOB_REF, "job_id": JOB_NUM}

    def fetchall(self):
        return self.rows


def test_lookup_index_queries_the_real_job_ids_not_the_column_names():
    svc = AutoAssignService()
    cur = _IndexCursor([_index_row()])
    idx = svc._build_candidate_lookup_index(cur, JOB_REF)
    _, params = cur.executed[-1]
    assert params == (JOB_REF, JOB_NUM)
    assert ADA_JD in idx["by_jcid"]
    assert EXA_ID in idx["by_candidate_id"]


# ---------------------------------------------------------------------------
# 2. how an applicant finds its origin row
# ---------------------------------------------------------------------------

def _index_with(*rows):
    svc = AutoAssignService()
    return svc, svc._build_candidate_lookup_index(_IndexCursor(list(rows)), JOB_REF)


def test_applicant_matches_the_provisioned_origin_row_by_stamp():
    svc, idx = _index_with(_index_row())
    hit = svc._find_in_index(idx, {"candidate_id": ADA_JD, "email": "ada@home.example.com"}, ADA_JD)
    assert hit is not None and hit["candidate_id"] == EXA_ID
    # a caller-supplied row carrying only the stamp resolves too
    hit = svc._find_in_index(idx, {"jobdiva_candidate_id": ADA_JD}, "")
    assert hit is not None and hit["candidate_id"] == EXA_ID


def test_synthetic_pair_email_matches_the_phone_only_origin_row():
    svc, idx = _index_with(_index_row(jcid=None, data={}, phone="(555) 123-4567", phone_norm="5551234567"))
    hit = svc._find_in_index(idx, {"candidate_id": "31", "email": "pair-5551234567@no-email.jobdiva.local"}, "31")
    assert hit is not None and hit["candidate_id"] == EXA_ID


def test_phone_matches_across_a_country_code():
    svc, idx = _index_with(_index_row(jcid=None, data={}, phone_norm="15551234567"))
    hit = svc._find_in_index(idx, {"candidate_id": "31", "phone": "555-123-4567"}, "31")
    assert hit is not None and hit["candidate_id"] == EXA_ID
    svc, idx = _index_with(_index_row(jcid=None, data={}, phone_norm="5551234567"))
    hit = svc._find_in_index(idx, {"candidate_id": "31", "phone": "+1 555 123 4567"}, "31")
    assert hit is not None and hit["candidate_id"] == EXA_ID


def test_linkedin_url_matches_after_normalisation():
    svc, idx = _index_with(_index_row(jcid=None, data={}, profile_url_norm="https://www.linkedin.com/in/ada/"))
    hit = svc._find_in_index(idx, {"candidate_id": "31", "profile_url": "linkedin.com/in/ada?trk=x"}, "31")
    assert hit is not None and hit["candidate_id"] == EXA_ID


def test_no_match_means_no_row():
    svc, idx = _index_with(_index_row())
    assert svc._find_in_index(idx, {"candidate_id": "31", "email": "nobody@example.com"}, "31") is None


# ---------------------------------------------------------------------------
# 3./4. the persisted blob
# ---------------------------------------------------------------------------

def test_matched_origin_row_keeps_its_score_and_explanation():
    svc = AutoAssignService()
    existing = {"match_score": 84, "explainability": ["Strong fit"], "jobdiva_application_origin": "pair"}
    payload = svc._build_candidate_payload(
        {"candidate_id": ADA_JD, "match_score": 0, "explainability": ["Scoring skipped (auto-assignment)"]},
        ADA_JD, existing, existing_source="LinkedIn-Exa",
    )
    assert payload["match_score"] == 84
    assert payload["explainability"] == ["Strong fit"]
    assert payload["jobdiva_application_origin"] == "pair"
    assert payload["auto_assigned"] is True


def test_real_incoming_score_still_wins():
    svc = AutoAssignService()
    payload = svc._build_candidate_payload(
        {"candidate_id": "555", "match_score": 71, "explainability": ["Good fit"]},
        "555", {"match_score": 40, "explainability": ["Old"]}, existing_source="JobDiva-Applicants",
    )
    assert payload["match_score"] == 71 and payload["explainability"] == ["Good fit"]


def test_new_applicant_is_organic_and_legacy_applicant_row_is_backfilled():
    svc = AutoAssignService()
    assert svc._build_candidate_payload({"candidate_id": "888"}, "888", None)["jobdiva_application_origin"] == "organic"
    legacy_applicant = svc._build_candidate_payload({"candidate_id": "555"}, "555", {}, existing_source="JobDiva-Applicants")
    assert legacy_applicant["jobdiva_application_origin"] == "organic"
    # an unknown legacy Exa row is not guessed at
    legacy_exa = svc._build_candidate_payload({"candidate_id": ADA_JD}, ADA_JD, {"jobdiva_candidate_id": ADA_JD}, existing_source="LinkedIn-Exa")
    assert "jobdiva_application_origin" not in legacy_exa


# ---------------------------------------------------------------------------
# End to end: one sync cycle over a job with a provisioned Exa person
# ---------------------------------------------------------------------------

class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self._result: Any = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self._conn.executed.append((flat, params))
        if "resume_match_filters" in flat:
            self._result = (None, None, JOB_REF, "open", "", "", "Acme Corp")
        elif "SELECT jobdiva_id, job_id FROM monitored_jobs" in flat:
            self._result = {"jobdiva_id": JOB_REF, "job_id": JOB_NUM}
        elif "FROM sourced_candidates" in flat:
            self._result = self._conn.index_rows
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []


class _Conn:
    def __init__(self, index_rows):
        self.index_rows = index_rows
        self.executed: List[Any] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self, **_kw):
        return _Cur(self)


def _emission(cid, **over):
    cand = {
        "candidate_id": cid, "id": cid, "jobdiva_candidate_id": cid, "source": "JobDiva-Applicants",
        "name": f"Person {cid}", "email": f"{cid}@example.com", "phone": "", "skills": ["Python"],
        "match_score": 0, "explainability": ["Scoring skipped (auto-assignment)"],
        "missing_skills": [], "matched_skills": [], "match_score_details": {},
    }
    cand.update(over)
    return cand


@pytest.fixture
def cycle(monkeypatch):
    """Runs one synchronize_job_applicants cycle with the DB, the search stream,
    the batch writer, the profile table and the auto-launch faked and recorded."""

    def run(index_rows, emissions):
        conn = _Conn(index_rows)
        writes: List[Any] = []
        profiles: List[Any] = []
        launches: List[Any] = []

        class _Search:
            async def search_candidates(self, criteria):
                assert criteria.sources == ["JobDiva Applicants"]
                for cand in emissions:
                    yield {"type": "candidate", "data": dict(cand)}
                yield {"type": "summary", "data": {"summary": {}}}

        def _execute_values(cur, sql, argslist, template=None, **_kw):
            writes.append({"sql": " ".join(sql.split()), "rows": list(argslist), "template": template})

        def _bulk(job, cands, source="JobDiva"):
            profiles.append({"job": job, "ids": [c.get("candidate_id") for c in cands], "source": source})

        def _auto_launch(ids, job):
            launches.append((list(ids), job))

            async def _noop():
                return None

            return _noop()

        async def _metrics(*_a, **_k):
            return None

        svc = AutoAssignService()
        monkeypatch.setattr(svc, "_get_db_connection", lambda: conn)
        monkeypatch.setattr(svc, "refresh_job_performance_metrics", _metrics)
        monkeypatch.setattr(aas, "unified_search_service", _Search())
        monkeypatch.setattr(aas.psycopg2.extras, "execute_values", _execute_values)
        monkeypatch.setattr(aas.candidate_profiles_db, "bulk_upsert_candidates", _bulk)
        import routers.engagement as eng
        monkeypatch.setattr(eng, "auto_launch_for_candidates", _auto_launch)

        total = asyncio.run(svc.synchronize_job_applicants(JOB_REF))
        return {"total": total, "conn": conn, "writes": writes, "profiles": profiles, "launches": launches}

    return run


def test_sync_cycle_updates_the_origin_row_and_inserts_only_real_applicants(cycle, caplog):
    caplog.set_level(logging.WARNING, logger="services.auto_assign_service")
    existing = [
        _index_row(),  # Ada: LinkedIn-Exa origin row, provisioned -> stamped with 777
        _index_row(id=2, candidate_id="555", source="JobDiva-Applicants", email="555@example.com",
                   email_lc="555@example.com", jcid=None, data={}, created_at=datetime(2026, 8, 1, tzinfo=timezone.utc)),
    ]
    emissions = [
        _emission(ADA_JD, name="Ada Lovelace", email="ada@home.example.com"),   # provisioned Exa person, other email
        _emission("555"),                                                        # organic applicant already synced
        _emission("888", phone="5551230000"),                                    # brand-new organic applicant
        _emission("999", jobdiva_application_meta={"resume_source": "PAIR"}),    # PAIR's own application, link lost
    ]
    result = cycle(existing, emissions)

    # The index was built for the job's real ids (the bug this file exists for).
    index_query = [p for sql, p in result["conn"].executed if "FROM sourced_candidates" in sql]
    assert index_query == [(JOB_REF, JOB_NUM)]

    updates = [w for w in result["writes"] if w["sql"].startswith("UPDATE sourced_candidates")]
    inserts = [w for w in result["writes"] if w["sql"].startswith("INSERT INTO sourced_candidates")]
    assert len(updates) == 1 and len(inserts) == 1

    # Ada and Bob were matched -> updated in place; the UPDATE never assigns `source`.
    updated_ids = {row[0] for row in updates[0]["rows"]}
    assert updated_ids == {1, 2}
    assert not re.search(r"\bsource\s*=", updates[0]["sql"])
    ada_blob = json.loads(next(row for row in updates[0]["rows"] if row[0] == 1)[7])
    assert ada_blob["match_score"] == 84                      # Step-5 score survives the placeholder 0
    assert ada_blob["explainability"] == ["Strong fit"]
    assert ada_blob["jobdiva_application_origin"] == "pair"   # provisioner's stamp survives
    assert ada_blob["jobdiva_candidate_id"] == ADA_JD
    bob_blob = json.loads(next(row for row in updates[0]["rows"] if row[0] == 2)[7])
    assert bob_blob["jobdiva_application_origin"] == "organic"

    # Only Carol (888) is a new applicant: inserted as organic, and the only one auto-launched.
    assert [(row[1], row[2]) for row in inserts[0]["rows"]] == [("888", "JobDiva-Applicants")]
    assert json.loads(inserts[0]["rows"][0][10])["jobdiva_application_origin"] == "organic"
    assert result["launches"] == [(["888"], JOB_REF)]
    assert result["total"] == 1

    # Dan (999) is PAIR's own application with no local row: skipped and logged, never re-imported.
    assert PAIR_APPLICATION_UNLINKED in caplog.text and "999" in caplog.text

    # The profile table only sees rows this sync owns -- never the Exa origin row.
    (profile_call,) = result["profiles"]
    assert profile_call["source"] == "JobDiva-Applicants"
    assert sorted(profile_call["ids"]) == ["555", "888"]


def test_sync_cycle_without_matches_inserts_everyone_as_organic(cycle):
    result = cycle([], [_emission("101"), _emission("102")])
    inserts = [w for w in result["writes"] if w["sql"].startswith("INSERT INTO sourced_candidates")]
    assert [row[1] for row in inserts[0]["rows"]] == ["101", "102"]
    assert all(json.loads(row[10])["jobdiva_application_origin"] == "organic" for row in inserts[0]["rows"])
    assert result["launches"] == [(["101", "102"], JOB_REF)]
    assert result["total"] == 2
