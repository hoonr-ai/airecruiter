"""Exa out-of-credits detection (the breaker's trigger).

Exa answers 402 with a tag once the account or a key's budget is spent. A 402
is not billed, so every Exa path used to fail open to an empty answer with
only a log line: Step 5 read like a thin market and Launch went ahead without
contacts (2026-10-13 19:56 UTC, ~49 h unnoticed on 9/21-23). The breaker on
``core.vendor_limiter.EXA`` trips on a match and Exa is skipped on every
worker until its TTL runs out (VENDOR_EXA_NO_CREDITS_TTL_S).

The match is strict on purpose: a false trip turns off ALL Exa (Pass A, Dice,
contacts) for the TTL. It needs status 402 AND one of the credit tags; other
402s (Exa's X402_* payment-protocol tags), 400s (a refused Fiber data source)
and 429s (rate limits) never trip it.
"""
from __future__ import annotations

import re
from typing import Any, Optional

OUT_OF_CREDITS_TAGS = ("NO_MORE_CREDITS", "API_KEY_BUDGET_EXCEEDED", "TEAM_BUDGET_EXCEEDED")

# Word boundaries: "X402_NO_MORE_CREDITS"-style tags must not match.
_TAG_RE = re.compile(r"\b(" + "|".join(OUT_OF_CREDITS_TAGS) + r")\b")
# The SDK raises ValueError("Request failed with status code 402: <body>").
_STATUS_RE = re.compile(r"status code (\d{3})\b")


class ExaOutOfCreditsError(RuntimeError):
    """Exa has no credits (or a key/team budget is spent). ``skipped`` = the
    breaker was already open and no request was sent."""

    def __init__(self, tag: str = "", *, skipped: bool = False):
        self.tag = tag or ""
        self.skipped = skipped
        what = "skipped while the out-of-credits breaker is open" if skipped else "out of credits"
        super().__init__(f"Exa is {what} ({self.tag or 'unknown'})")


class ExaNoSlotError(RuntimeError):
    """No Exa limiter slot came up within the caller's wait budget (nothing sent)."""


def credit_tag(status: Optional[int], body: Any) -> str:
    """The out-of-credits tag of a response (status + body), else ""."""
    if status != 402:
        return ""
    m = _TAG_RE.search(str(body or ""))
    return m.group(1) if m else ""


def http_status_of(exc: BaseException) -> Optional[int]:
    """Status code carried by an SDK / httpx exception, else parsed from the
    SDK's "status code NNN" message, else None (same order as _is_exa_429)."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    m = _STATUS_RE.search(str(exc))
    return int(m.group(1)) if m else None


def out_of_credits_tag(exc: BaseException) -> str:
    """The credit tag when ``exc`` is an out-of-credits answer, else ""."""
    if isinstance(exc, ExaOutOfCreditsError):
        return exc.tag or "NO_MORE_CREDITS"
    body = f"{exc} {getattr(getattr(exc, 'response', None), 'text', '') or ''}"
    return credit_tag(http_status_of(exc), body)


def is_exa_out_of_credits(exc: BaseException) -> bool:
    return bool(out_of_credits_tag(exc))
