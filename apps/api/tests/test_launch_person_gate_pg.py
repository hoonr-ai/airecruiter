"""The launch person gate (routers/engagement.py ``_person_gate``) against a
real Postgres: one human is never claimed twice for a job, whatever
candidate_id each copy carries — on the legacy send claim, the session claim,
and when two claims for different ids of one person race.

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, falling back
to LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").
"""
import json

import pytest

from routers import engagement as eng
from tests.test_launch_platform_sql_pg import (  # noqa: F401 — fixtures
    JOB,
    REF,
    _data,
    _race,
    pg,
    pg_pair,
)


def _person(conn, cid, *, data=None, job=JOB, source="LinkedIn-Exa", email=None, phone=None, profile_url=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, email, phone, profile_url, data)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (job, cid, source, email, phone, profile_url, json.dumps(data or {})),
        )


def _audit_with_email(conn, cid, interview_id, email, job=REF):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, candidate_email, status)"
            " VALUES (%s, %s, %s, %s, 'Initiated')",
            (cid, job, interview_id, email),
        )


LAUNCHED = {"engage_status": "sent", "engage_interview_id": "iv-jane"}


@pytest.mark.parametrize("held, copy", [
    (dict(email="jane@x.com"), dict(email="JANE@x.com ")),
    (dict(phone="(415) 555-0142"), dict(phone="+1 415 555 0142")),
    (dict(profile_url="https://www.linkedin.com/in/jane-d"), dict(profile_url="http://linkedin.com/in/Jane-D/")),
    (dict(data={"jobdiva_candidate_id": "4471"}), dict(source="JobDiva-Applicant")),
])
def test_a_second_id_of_a_launched_person_is_never_claimed(pg, held, copy):
    held = dict(held)
    _person(pg, "exa_jane", data={**LAUNCHED, **held.pop("data", {})}, **held)
    copy_id = "4471" if copy.get("source") == "JobDiva-Applicant" else "unipile_jane"
    _person(pg, copy_id, **copy)
    _person(pg, "bob", email="bob@x.com")

    claimed, with_rows, dups = eng._claim_rows_for_send((JOB, REF), [copy_id, "bob"], None)
    assert claimed == ["bob"]
    assert dups == {copy_id: ("exa_jane", dups[copy_id][1])}
    assert copy_id not in with_rows                          # never reported as row-less
    assert _data(pg, copy_id) == {}                          # nothing written for the copy
    assert eng.claim_candidates_for_session((JOB, REF), [copy_id], "S-1") == []


def test_in_flight_and_audit_only_people_hold_the_person_too(pg):
    _person(pg, "a1", email="a@x.com", data={"engage_status": "processing"})
    _person(pg, "a2", email="a@x.com")
    _audit_with_email(pg, "gone", "iv-gone", "Gone@X.com")              # no sourced row left
    _person(pg, "g2", email="gone@x.com")
    _person(pg, "f1", email="f@x.com", data={"engage_status": "failed"})  # a failed launch holds nobody
    _person(pg, "f2", email="f@x.com")
    claimed, _, dups = eng._claim_rows_for_send((JOB, REF), ["a2", "g2", "f2"], None)
    assert claimed == ["f2"]
    assert dups == {"a2": ("a1", "email"), "g2": ("gone", "email")}


def test_two_copies_in_one_request_send_the_first_only(pg):
    _person(pg, "a", phone="415-555-0142", email="a@work.com")
    _person(pg, "b", phone="4155550142", email="a@home.com")
    _person(pg, "c", email="c@x.com")
    duplicates = {}
    assert eng.claim_candidates_for_session((JOB, REF), ["a", "b", "c"], "S-1", duplicates=duplicates) == ["a", "c"]
    assert duplicates == {"b": ("a", "phone")}
    assert _data(pg, "b") == {}


def test_a_sessions_own_queued_siblings_do_not_block_each_other(pg):
    _person(pg, "a", email="p@x.com")
    _person(pg, "z", email="z@x.com")
    assert eng.claim_candidates_for_session((JOB, REF), ["a", "z"], "S-1") == ["a", "z"]
    # Both are 'queued' by S-1; its send claim takes them, batch by batch.
    assert eng._claim_rows_for_send((JOB, REF), ["z"], "S-1")[0] == ["z"]
    assert eng._claim_rows_for_send((JOB, REF), ["a"], "S-1")[0] == ["a"]
    # Another session / legacy path sees a held person.
    _person(pg, "a-copy", email="p@x.com")
    assert eng._claim_rows_for_send((JOB, REF), ["a-copy"], "S-2")[2] == {"a-copy": ("a", "email")}


def test_a_held_person_on_either_job_key_blocks(pg):
    _person(pg, "exa_jane", email="jane@x.com", data=LAUNCHED, job=REF)
    _person(pg, "4471", email="jane@x.com", source="JobDiva-Applicant")
    assert eng._claim_rows_for_send((JOB, REF), ["4471"], None)[2] == {"4471": ("exa_jane", "email")}


def test_another_jobs_launch_does_not_block(pg):
    _person(pg, "exa_jane", email="jane@x.com", data=LAUNCHED, job="OTHER-JOB")
    _person(pg, "4471", email="jane@x.com", source="JobDiva-Applicant")
    assert eng._claim_rows_for_send((JOB, REF), ["4471"], None)[0] == ["4471"]


def test_a_rowless_legacy_id_is_gated_on_its_payload_contacts(pg):
    _person(pg, "exa_jane", email="jane@x.com", data=LAUNCHED)
    claimed, with_rows, dups = eng._claim_rows_for_send(
        (JOB, REF), ["ghost", "ghost2"], None,
        {"ghost": {"email": "jane@x.com"}, "ghost2": {"email": "new@x.com"}},
    )
    assert claimed == [] and with_rows == set()
    assert dups == {"ghost": ("exa_jane", "email")}


def test_two_claims_for_two_ids_of_one_person_never_both_win(pg_pair):
    admin, workers, local = pg_pair
    with admin.cursor() as cur:
        for cid in ("exa_jane", "4471"):
            cur.execute(
                "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, email, data) VALUES (%s, %s, %s, '{}'::jsonb)",
                (JOB, cid, "jane@x.com"),
            )
    first, second = _race(
        admin, workers, local,
        (eng._claim_rows_for_send, (JOB, REF), ["exa_jane"], None),
        (eng.claim_candidates_for_session, (JOB, REF), ["4471"], "S-1"),
    )
    assert first[0] == ["exa_jane"]
    assert second == []                     # blocked on the per-job lock, then saw 'sending'


# ---------------------------------------------------------------------------
# settle_parked_legacy_rows: rows a launch outside a session left parked
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import datetime as _dt  # noqa: E402


def _parked(conn, cid, *, minutes_ago, status="processing", parked_hours_ago=None, session=""):
    parked_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=parked_hours_ago or 0, minutes=minutes_ago)
    data = {"engage_status": status, "engage_updated_at": parked_at.isoformat(), "engage_bulk_id": "bulk-1"}
    if session:
        data["engage_session_id"] = session
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, data, updated_at)"
            " VALUES (%s, %s, %s::jsonb, now() - make_interval(mins => %s))",
            (REF, cid, json.dumps(data), int(minutes_ago + 60 * (parked_hours_ago or 0))),
        )


def test_settle_sweep_settles_parked_legacy_rows_against_by_job(pg, monkeypatch):
    _parked(pg, "proven", minutes_ago=15)
    _parked(pg, "twin", minutes_ago=15)
    _parked(pg, "young_missing", minutes_ago=15)
    _parked(pg, "old_missing", minutes_ago=0, parked_hours_ago=25)
    _parked(pg, "stuck_sending", minutes_ago=45, status="sending")
    _parked(pg, "fresh", minutes_ago=2)                           # not due yet
    _parked(pg, "fresh_sending", minutes_ago=15, status="sending")  # sending: due after 30 min only
    _parked(pg, "session_row", minutes_ago=60, session="S-1")     # the session's housekeeping owns it
    asked = []

    async def by_job(job_key):
        asked.append(job_key)
        return {"bulk_in_progress": False, "interviews": [
            {"interview_id": "iv-proven", "source_candidate_id": "proven"},
            {"interview_id": "iv-twin", "source_candidate_id": "twin-other-id"},
        ]}

    monkeypatch.setattr(eng, "_fetch_pairbot_job_interviews", by_job)
    monkeypatch.setattr(eng, "jobdiva_provisioning_enabled", lambda: False)
    real_reconcile = eng.reconcile_with_pairbot

    async def reconcile(keys, ids, **kw):
        # The twin is listed under another PAIR id for this job.
        out = await real_reconcile(keys, ids, existing_interview_ids={"twin": "iv-twin"}, **kw)
        return out

    monkeypatch.setattr(eng, "reconcile_with_pairbot", reconcile)
    out = asyncio.run(eng.settle_parked_legacy_rows())
    assert out["checked"] == 5
    assert _data(pg, "proven", job=REF)["engage_status"] == "sent"
    assert _data(pg, "proven", job=REF)["engage_interview_id"] == "iv-proven"
    twin = _data(pg, "twin", job=REF)
    assert twin["engage_status"] == "failed" and twin["engage_block_reason"] == "duplicate_person_this_job"
    assert _data(pg, "young_missing", job=REF)["engage_status"] == "processing"   # one 'missing' is not proof
    old = _data(pg, "old_missing", job=REF)
    assert old["engage_status"] == "failed" and old["engage_last_response"] == {"settled": "not_created_at_pairbot"}
    assert _data(pg, "stuck_sending", job=REF)["engage_status"] == "sending"     # checked, young: stays held
    for untouched in ("fresh", "fresh_sending", "session_row"):
        assert "engage_settle_checked_at" not in _data(pg, untouched, job=REF), untouched
    # Each row is checked at most every 10 min: an immediate second sweep asks nobody.
    asked.clear()
    assert asyncio.run(eng.settle_parked_legacy_rows())["checked"] == 0 and asked == []
