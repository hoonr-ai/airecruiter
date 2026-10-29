"""End-to-end coverage for the Candidate Passed email's resume attachment
(PR #869 review): test_jobdiva_resume_file.py and
test_pass_email_resume_attachment.py each cover `get_resume_file` and
`_resolve_pass_email_resume_attachment` in isolation, with the other side
faked out. This file wires the REAL `get_resume_file` (HTTP mocked only at
the transport boundary, same as test_jobdiva_resume_file.py) into the REAL
`_resolve_pass_email_resume_attachment`, so a regression at the seam between
them — the shape `get_resume_file` returns vs. what its caller expects —
would show up here even if each side's own unit tests still pass.

Three paths in one flow: the original file attached as-is, the allowlist/
magic-byte rejection path falling through to the generated .doc, and the
generated .doc itself when there's no JobDiva resume at all.
"""
import asyncio
import base64

import httpx

from services import jobdiva
from services.jobdiva import JobDivaService
from routers.engagement.provisioning import _resolve_pass_email_resume_attachment

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


def test_e2e_original_pdf_file_is_attached_as_is(monkeypatch):
    _patch_auth(monkeypatch)
    raw_pdf = b"%PDF-1.7\nreal resume content"
    content = base64.b64encode(raw_pdf).decode()
    _patch_bi_get(monkeypatch, httpx.Response(200, json={
        "data": [{"FILENAME": "../HEATHER KUSNITZ-PM26.pdf", "FILECONTENT_BASE64ENCODED": content}]
    }))

    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, "123_1_1", used_local_resume_fallback=False,
            resume_text="Heather's parsed resume text", candidate_name="Heather Kusnitz",
            job_id="26-12137",
        )
    )

    assert resume_bytes == raw_pdf
    assert filename == "HEATHER KUSNITZ-PM26.pdf"  # path-stripped, same file
    assert content_type == "application/pdf"
    assert is_original is True


def test_e2e_magic_byte_mismatch_falls_through_to_generated_doc(monkeypatch):
    """JobDiva's row claims .pdf but the decoded bytes aren't a PDF at all —
    get_resume_file must reject it (not attach a mismatched file as-is), and
    the caller must fall all the way through to the generated .doc rather
    than attaching nothing."""
    _patch_auth(monkeypatch)
    content = base64.b64encode(b"this is not actually a pdf").decode()
    _patch_bi_get(monkeypatch, httpx.Response(200, json={
        "data": [{"FILENAME": "resume.pdf", "FILECONTENT_BASE64ENCODED": content}]
    }))

    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, "123_1_1", used_local_resume_fallback=False,
            resume_text="Heather's parsed resume text", candidate_name="Heather Kusnitz",
            job_id="26-12137",
        )
    )

    assert resume_bytes is not None
    assert filename == "Resume_Heather_Kusnitz_26-12137.doc"
    assert content_type == "application/msword"
    assert is_original is False


def test_e2e_disallowed_extension_falls_through_to_generated_doc(monkeypatch):
    """A resume filename outside the allowlist (e.g. an .exe the candidate
    somehow uploaded) must never reach the email; same fallback as the
    magic-byte mismatch above."""
    _patch_auth(monkeypatch)
    content = base64.b64encode(b"MZ\x90\x00fake executable bytes").decode()
    _patch_bi_get(monkeypatch, httpx.Response(200, json={
        "data": [{"FILENAME": "resume.exe", "FILECONTENT_BASE64ENCODED": content}]
    }))

    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, "123_1_1", used_local_resume_fallback=False,
            resume_text="Heather's parsed resume text", candidate_name="Heather Kusnitz",
            job_id="26-12137",
        )
    )

    assert filename == "Resume_Heather_Kusnitz_26-12137.doc"
    assert content_type == "application/msword"
    assert is_original is False


def test_e2e_no_jobdiva_resume_id_generates_doc_from_local_text():
    """No JobDiva resume at all (locally-sourced candidate) — get_resume_file
    is never even called; the .doc is built straight from the locally
    stored resume text."""
    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, None, used_local_resume_fallback=True,
            resume_text="Locally stored resume text", candidate_name="Heather Kusnitz",
            job_id="26-12137",
        )
    )

    assert resume_bytes is not None
    assert filename == "Resume_Heather_Kusnitz_26-12137.doc"
    assert content_type == "application/msword"
    assert is_original is False
