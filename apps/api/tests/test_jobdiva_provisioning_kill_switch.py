"""JOBDIVA_PROVISIONING_ENABLED (routers/engagement.py): the kill switch
every JobDiva provisioning path honours — the inline per-batch provisioning
(_provision_batch_to_jobdiva, which /engage/re-provision also uses) and the
launch-session stage S7b (provision_sent_candidates). Default on."""

import asyncio

import pytest

from routers import engagement as eng
from routers.engagement import provisioning as eng_provisioning  # noqa: E402


def _boom(*_a, **_k):
    raise AssertionError("must not be reached while provisioning is disabled")


async def _aboom(*_a, **_k):
    raise AssertionError("must not be reached while provisioning is disabled")


@pytest.mark.parametrize("value", ["false", "0", "off", "no", " FALSE "])
def test_disabled_values(monkeypatch, value):
    monkeypatch.setenv("JOBDIVA_PROVISIONING_ENABLED", value)
    assert eng.jobdiva_provisioning_enabled() is False


def test_default_is_on(monkeypatch):
    monkeypatch.delenv("JOBDIVA_PROVISIONING_ENABLED", raising=False)
    assert eng.jobdiva_provisioning_enabled() is True
    monkeypatch.setenv("JOBDIVA_PROVISIONING_ENABLED", "true")
    assert eng.jobdiva_provisioning_enabled() is True


def test_inline_batch_provisioning_writes_nothing_when_disabled(monkeypatch):
    monkeypatch.setenv("JOBDIVA_PROVISIONING_ENABLED", "false")
    monkeypatch.setattr(eng_provisioning, "_resolve_provisioning_job_ids", _aboom)
    monkeypatch.setattr(eng_provisioning, "get_db_connection", _boom, raising=False)
    res = asyncio.run(eng._provision_batch_to_jobdiva(["c1", "c2"], "J1"))
    assert res == {"success": 0, "skipped": 2, "failed": 0, "duplicate_suspected": 0, "blank_profile_refused": 0}


def test_session_provisioning_claims_nothing_when_disabled(monkeypatch):
    monkeypatch.setenv("JOBDIVA_PROVISIONING_ENABLED", "false")
    monkeypatch.setattr(eng_provisioning, "_resolve_provisioning_job_ids", _aboom)
    monkeypatch.setattr(eng_provisioning, "_claim_rows_for_provisioning", _boom)
    out = asyncio.run(eng.provision_sent_candidates("J1", ["c1", "c2"]))
    assert out["claimed"] == 0 and out["not_claimed"] == 2 and out["success"] == 0 and out["skipped"] == 2


def test_session_provisioning_runs_when_enabled(monkeypatch):
    monkeypatch.delenv("JOBDIVA_PROVISIONING_ENABLED", raising=False)
    calls = []

    async def resolve(job_id):
        return "1001", "26-001"

    async def provision(ids, job_id, label="x"):
        calls.append(list(ids))
        return {"success": len(ids)}

    monkeypatch.setattr(eng_provisioning, "_resolve_provisioning_job_ids", resolve)
    monkeypatch.setattr(eng_provisioning, "_claim_rows_for_provisioning", lambda keys, ids: list(ids))
    monkeypatch.setattr(eng_provisioning, "_provision_batch_to_jobdiva", provision)
    out = asyncio.run(eng.provision_sent_candidates("26-001", ["c1", "c2"]))
    assert calls == [["c1", "c2"]] and out["success"] == 2
