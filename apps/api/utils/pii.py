"""Masking helpers for contact details in log lines.

Application logs travel further and live longer than the tables they describe
— shipped to an aggregator, retained past the DB's own window, readable by
anyone with log access rather than anyone with row access. A candidate's email
address and phone number do not need to make that trip for a log line to be
useful, and the do-not-contact flow is the last place they should: the whole
feature exists because someone asked us to stop holding onto them.

Enough is kept to correlate a line with a specific case (the email domain, the
last four digits); the full values live in ``outreach_opt_out_audit`` and
``dnc_list`` for whoever needs to trace one.
"""

from __future__ import annotations

from typing import Optional


def mask_email(email: Optional[str]) -> str:
    """'ahmay02@gmail.com' -> 'a***@gmail.com'. Falsy input -> '-'."""
    if not email:
        return "-"
    value = str(email).strip()
    if "@" not in value:
        # Not an address; reveal nothing but its shape.
        return f"***({len(value)} chars)"
    local, _, domain = value.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def mask_phone(phone: Optional[str]) -> str:
    """'+1 (510) 590-8688' -> '***8688'. Falsy input -> '-'.

    The last four are what a recruiter reads back off a screen when matching a
    log line to the row they were looking at, and are not identifying alone.
    """
    if not phone:
        return "-"
    digits = "".join(c for c in str(phone) if c.isdigit())
    if not digits:
        return "***"
    return f"***{digits[-4:]}"
