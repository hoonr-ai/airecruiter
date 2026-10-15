"""The Launch deep-email holdout's decision log (services/contact_holdout.py,
EXA_DEEP_SKIP_REACHABLE_PCT, 2026-10-14): one row per person, written at the
decision in both arms (first exposure wins), and the control arm's deep
outcome. The SQL cases need a real Postgres (LAUNCH_REPORT_TEST_DSN); the
table lives in a schema of its own.
"""
import asyncio
import logging
import os
import uuid

import psycopg2
import pytest

from services import contact_holdout

# conftest swaps these for no-ops in every test; these are the real ones.
RECORD_DECISION = contact_holdout.record_decision
RECORD_DEEP_OUTCOME = contact_holdout.record_deep_outcome

JANE = "https://www.linkedin.com/in/jane-doe"
ALEX = "https://www.linkedin.com/in/Alex-Smith/?trk=x"
_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


@pytest.fixture
def pgs(monkeypatch):
    try:
        admin = psycopg2.connect(_DSN, connect_timeout=2)
    except Exception:
        pytest.skip("no Postgres reachable (set LAUNCH_REPORT_TEST_DSN)")
    admin.autocommit = True
    schema = f"cc_holdout_{uuid.uuid4().hex[:10]}"
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")

    def _connect():
        return psycopg2.connect(_DSN, connect_timeout=2, options=f"-c search_path={schema}")

    monkeypatch.setattr(contact_holdout, "_connect", _connect)
    monkeypatch.setattr(contact_holdout, "_table_ready", False)

    def sql(query, params=None):
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall() if cur.description else None
            conn.commit()
            return rows
        finally:
            conn.close()

    yield sql
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def _rows(sql):
    return sql(
        "SELECT linkedin_slug, arm, pct, jobdiva_id, candidate_id, phone_from, normal_outcome, deep_outcome "
        "FROM contact_deep_skip_holdout ORDER BY linkedin_slug"
    )


def test_the_first_exposure_of_each_arm_is_logged_once(pgs):
    async def _go():
        return [
            await RECORD_DECISION(JANE, arm="skip", pct=50, jobdiva_id="26-1", candidate_id="c1",
                                  phone_from="found", normal_outcome="phone"),
            await RECORD_DECISION(ALEX, arm="control", pct=50, jobdiva_id=None, candidate_id="c2",
                                  phone_from="seed", normal_outcome="-"),
            # A later Launch of the same person (another job, another worker):
            # the first exposure stands.
            await RECORD_DECISION(JANE, arm="skip", pct=60, jobdiva_id="26-9", candidate_id="c9",
                                  phone_from="seed", normal_outcome="miss"),
        ]

    assert asyncio.run(_go()) == [True, True, False]
    assert _rows(pgs) == [
        ("alex-smith", "control", 50, None, "c2", "seed", "-", None),
        ("jane-doe", "skip", 50, "26-1", "c1", "found", "phone", None),
    ]


def test_racing_lookups_of_one_person_make_one_row(pgs):
    async def _go():
        return await asyncio.gather(*(
            RECORD_DECISION(ALEX, arm="control", pct=50, candidate_id=f"c{i}") for i in range(6)
        ))

    assert sorted(asyncio.run(_go())) == [False] * 5 + [True]
    assert pgs("SELECT count(*) FROM contact_deep_skip_holdout") == [(1,)]


def test_the_control_arms_first_definitive_deep_outcome_wins(pgs):
    async def _go():
        await RECORD_DECISION(JANE, arm="skip", pct=50)
        await RECORD_DECISION(ALEX, arm="control", pct=50)
        await RECORD_DEEP_OUTCOME(JANE, "email")          # a skip row has no deep outcome
        await RECORD_DEEP_OUTCOME(ALEX, "in_progress")
        await RECORD_DEEP_OUTCOME(ALEX, "failed")         # replaces "in_progress"
        await RECORD_DEEP_OUTCOME(ALEX, "email")          # replaces "failed"
        await RECORD_DEEP_OUTCOME(ALEX, "miss")           # a definitive answer stands
        await RECORD_DEEP_OUTCOME(ALEX, "nonsense")       # not an outcome: ignored

    asyncio.run(_go())

    assert [(r[0], r[7]) for r in _rows(pgs)] == [("alex-smith", "email"), ("jane-doe", None)]
    [(at,)] = pgs("SELECT deep_outcome_at FROM contact_deep_skip_holdout WHERE arm = 'control'")
    assert at is not None


def test_nothing_is_logged_without_a_slug_or_an_arm(pgs):
    async def _go():
        return [
            await RECORD_DECISION("https://example.com/jane", arm="skip", pct=50),
            await RECORD_DECISION(JANE, arm="off", pct=0),
        ]

    assert asyncio.run(_go()) == [None, None]
    assert pgs("SELECT to_regclass('contact_deep_skip_holdout')") == [(None,)]


def test_a_database_problem_never_fails_the_lookup(monkeypatch, caplog):
    def _down():
        raise RuntimeError("db down")

    monkeypatch.setattr(contact_holdout, "_connect", _down)
    monkeypatch.setattr(contact_holdout, "_table_ready", False)

    async def _go():
        got = await RECORD_DECISION(JANE, arm="skip", pct=50)
        await RECORD_DEEP_OUTCOME(JANE, "email")
        return got

    with caplog.at_level(logging.WARNING, logger="services.contact_holdout"):
        assert asyncio.run(_go()) is None
    assert sum("contact_holdout" in r.getMessage() for r in caplog.records) == 2
