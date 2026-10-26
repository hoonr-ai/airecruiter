"""Regression guard for PR #840: the two newest rejection reasons shipped on
the frontend (apps/web/lib/rejection.ts's REJECTION_REASONS) with no matching
entry in the backend's REJECTION_REASON_TO_JOBDIVA_ACTION
(routers/candidates.py) — selecting either one in the UI and confirming the
rejection failed with a 422, since save_candidate_feedback validates
`request.reason` against that mapping.

There's no single file both sides can import (one's TS, one's Python), so
this test parses the frontend's array as text and asserts every entry in it
has a matching key on the backend — the next time someone adds a UI-only
reason without updating the backend, this fails in CI instead of in
production.
"""
import re
from pathlib import Path

from routers.candidates import REJECTION_REASON_TO_JOBDIVA_ACTION

_REJECTION_TS = Path(__file__).resolve().parents[2] / "web" / "lib" / "rejection.ts"


def _frontend_reasons() -> list[str]:
    text = _REJECTION_TS.read_text()
    # Each entry is a double-quoted JS string literal, one per line, inside
    # the REJECTION_REASONS array — no escaped quotes appear in any entry
    # today, so a plain non-greedy match between quotes is sufficient.
    return re.findall(r'"\s*([^"]+?)\s*",?\s*\n', text.split("REJECTION_REASONS = [", 1)[1])


def test_rejection_ts_path_exists():
    # Fails loudly (not just "0 reasons found") if the frontend file ever
    # moves, so this test can't silently pass with an empty list forever.
    assert _REJECTION_TS.is_file(), f"expected {_REJECTION_TS} to exist"


def test_every_frontend_reason_has_a_backend_mapping():
    frontend_reasons = _frontend_reasons()
    assert len(frontend_reasons) >= 20, (
        f"only parsed {len(frontend_reasons)} reasons out of lib/rejection.ts — "
        "the regex above may no longer match its format"
    )
    missing = [r for r in frontend_reasons if r not in REJECTION_REASON_TO_JOBDIVA_ACTION]
    assert missing == [], (
        f"{missing} appear in apps/web/lib/rejection.ts but have no entry in "
        "REJECTION_REASON_TO_JOBDIVA_ACTION (routers/candidates.py) — selecting "
        "one of these in the UI will fail with a 422 on confirm"
    )
