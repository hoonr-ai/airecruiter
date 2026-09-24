"""Shared auth header builder for calls to the PAIR (pairbot) API.

The `PAIR_API_KEY` -> `Authorization: Bearer ...` pattern was copy-pasted
across engagement.py, candidates.py, and launch_report.py. That duplication
is how a fan-out of parallel PAIR requests ended up with the header on only
one of the calls in a `gather`/parallel block instead of all of them - each
call site had to remember the same three lines instead of getting it by
default. Route new PAIR call sites through here instead of re-deriving the
header inline.
"""

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

_warned_missing_key = False


def get_pair_auth_headers(*, json_content_type: bool = False) -> Dict[str, str]:
    """Build headers for a request to the PAIR API.

    Adds `Authorization: Bearer <PAIR_API_KEY>` when the key is set. When it
    is not, logs a warning once per process (instead of once per call) so a
    missing key is traceable in logs rather than only discoverable via
    downstream 401s.
    """
    global _warned_missing_key
    headers: Dict[str, str] = {"Content-Type": "application/json"} if json_content_type else {}
    pair_api_key = os.getenv("PAIR_API_KEY", "").strip()
    if pair_api_key:
        headers["Authorization"] = f"Bearer {pair_api_key}"
    elif not _warned_missing_key:
        logger.warning("pair_api_key_unset: PAIR_API_KEY is not set; PAIR API requests will be sent unauthenticated")
        _warned_missing_key = True
    return headers
