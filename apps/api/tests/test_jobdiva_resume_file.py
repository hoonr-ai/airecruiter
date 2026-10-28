"""Coverage for `get_resume_file` / `_sanitize_resume_filename` (PR #867 review):
the original-resume-file path forwards a JobDiva-supplied filename and
base64 blob straight into a recruiter email attachment, so it needs to fail
closed (return None -> caller falls back to the generated .doc) on missing
data, bad base64, disallowed file types, and oversized payloads, and it must
never leak a path or control characters into the attachment name.
"""
import asyncio
import base64

import httpx

from services import jobdiva
from services.jobdiva import JobDivaService, _sanitize_resume_filename

svc = JobDivaService()


def _run(coro):
    return asyncio.run(coro)


def _patch_auth(monkeypatch, token="fake-token"):
    async def fake_authenticate(force_refresh=False):
        return token
    monkeypatch.setattr(svc, "authenticate", fake_authenticate)


def _patch_bi_get(monkeypatch, response):
    async def fake_bi_get(client, url, *, max_wait_s=120.0, priority="interactive", label="bi", **kwargs):
        return response
    monkeypatch.setattr(jobdiva._bi_rate_limit, "bi_get", fake_bi_get)


# --- _sanitize_resume_filename -----------------------------------------

def test_sanitize_filename_strips_path_components():
    assert _sanitize_resume_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert _sanitize_resume_filename("C:\\Users\\x\\Resume.docx") == "Resume.docx"


def test_sanitize_filename_strips_control_and_unsafe_chars():
    name = _sanitize_resume_filename("resume\x00\n<script>.pdf")
    assert name == "resume___script_.pdf"
    assert all(c.isprintable() and c not in "<>" for c in name)


def test_sanitize_filename_caps_length_but_keeps_extension():
    name = _sanitize_resume_filename("a" * 300 + ".pdf", max_length=20)
    assert len(name) == 20
    assert name.endswith(".pdf")


def test_sanitize_filename_falls_back_when_empty():
    assert _sanitize_resume_filename("") == "resume"
    assert _sanitize_resume_filename(None) == "resume"


# --- get_resume_file -----------------------------------------------------

def test_get_resume_file_returns_none_without_resume_id():
    assert _run(svc.get_resume_file("")) is None
    assert _run(svc.get_resume_file(None)) is None


def test_get_resume_file_returns_none_when_auth_fails(monkeypatch):
    async def fake_authenticate(force_refresh=False):
        return None
    monkeypatch.setattr(svc, "authenticate", fake_authenticate)
    assert _run(svc.get_resume_file("123_1_1")) is None


def test_get_resume_file_returns_none_on_non_200(monkeypatch):
    _patch_auth(monkeypatch)
    _patch_bi_get(monkeypatch, httpx.Response(500, json={}))
    assert _run(svc.get_resume_file("123_1_1")) is None


def test_get_resume_file_returns_none_on_empty_rows(monkeypatch):
    _patch_auth(monkeypatch)
    _patch_bi_get(monkeypatch, httpx.Response(200, json={"data": []}))
    assert _run(svc.get_resume_file("123_1_1")) is None


def test_get_resume_file_returns_none_on_missing_fields(monkeypatch):
    _patch_auth(monkeypatch)
    _patch_bi_get(monkeypatch, httpx.Response(200, json={"data": [{"FILENAME": "resume.pdf"}]}))
    assert _run(svc.get_resume_file("123_1_1")) is None


def test_get_resume_file_returns_none_on_bad_base64(monkeypatch):
    _patch_auth(monkeypatch)
    _patch_bi_get(monkeypatch, httpx.Response(200, json={
        "data": [{"FILENAME": "resume.pdf", "FILECONTENT_BASE64ENCODED": "not-valid-base64!!!"}]
    }))
    assert _run(svc.get_resume_file("123_1_1")) is None


def test_get_resume_file_returns_none_on_disallowed_extension(monkeypatch):
    _patch_auth(monkeypatch)
    content = base64.b64encode(b"#!/bin/sh\necho hi").decode()
    _patch_bi_get(monkeypatch, httpx.Response(200, json={
        "data": [{"FILENAME": "resume.sh", "FILECONTENT_BASE64ENCODED": content}]
    }))
    assert _run(svc.get_resume_file("123_1_1")) is None


def test_get_resume_file_returns_none_when_over_size_limit(monkeypatch):
    _patch_auth(monkeypatch)
    big = base64.b64encode(b"x" * (jobdiva._MAX_RESUME_ATTACHMENT_BYTES + 1)).decode()
    _patch_bi_get(monkeypatch, httpx.Response(200, json={
        "data": [{"FILENAME": "resume.pdf", "FILECONTENT_BASE64ENCODED": big}]
    }))
    assert _run(svc.get_resume_file("123_1_1")) is None


def test_get_resume_file_happy_path_returns_sanitized_filename_and_bytes(monkeypatch):
    _patch_auth(monkeypatch)
    raw_bytes = b"%PDF-1.7\nfake pdf content"
    content = base64.b64encode(raw_bytes).decode()
    _patch_bi_get(monkeypatch, httpx.Response(200, json={
        "data": [{"FILENAME": "../HEATHER KUSNITZ-PM26.pdf", "FILECONTENT_BASE64ENCODED": content}]
    }))
    result = _run(svc.get_resume_file("123_1_1"))
    assert result == {"filename": "HEATHER KUSNITZ-PM26.pdf", "content": raw_bytes}
