"""Confirms the SMTP send path properly encodes a non-ASCII attachment
filename (PR #870 review: _safe_filename_stem keeps any Unicode
alphanumerics in the generated .doc's name — does the email builder encode
that correctly, or would some clients show a garbled name?).

Checked directly against Python's email package rather than assumed:
Message.add_header("Content-Disposition", "attachment", filename=...) with a
non-ASCII str value emits RFC 2231 encoding (filename*=utf-8''...)
automatically — core.email._send's SMTP fallback already calls add_header
exactly that way, so no code change was needed here; this test pins the
behavior down so a future refactor (e.g. building the header string by hand
instead of via add_header's filename= kwarg) can't silently regress it.

The MS Graph path (core.email._send_via_msgraph) sends the filename as a
plain JSON string value, which is natively Unicode-safe — nothing to encode.
"""
import smtplib

from core import email as email_mod


class _FakeSMTP:
    """Captures the message that would be sent, raises nothing, connects
    nowhere."""
    sent = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *args, **kwargs):
        pass

    def sendmail(self, from_addr, to_addrs, msg_string):
        _FakeSMTP.sent = {"from": from_addr, "to": to_addrs, "msg": msg_string}


def test_smtp_attachment_filename_is_rfc2231_encoded_for_non_ascii_names(monkeypatch):
    monkeypatch.setattr(email_mod, "_ms_graph_configured", lambda: False)
    monkeypatch.setattr(email_mod, "_smtp_configured", lambda: True)
    monkeypatch.setattr(email_mod, "SMTP_USE_SSL", False)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = {}

    result = email_mod._send(
        ["recruiter@pyramidci.com"],
        "Subject",
        "<p>html</p>",
        "plain",
        attachments=[{
            "filename": "Resume_José_García_456.doc",
            "content": b"fake doc bytes",
            "content_type": "application/msword",
        }],
    )

    assert result is True
    msg = _FakeSMTP.sent["msg"]
    assert "filename*=utf-8''" in msg
    assert "Jos%C3%A9" in msg and "Garc%C3%ADa" in msg
