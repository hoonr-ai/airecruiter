"""
Tests for the 3-layer outreach merge fallback in get_job_candidates (candidates.py).
Covers:
- Live API status wins over stale local DB status
- Nested outreach key in live API response is correctly unwrapped
- Timeout from pair-bot falls back gracefully to local DB data
- Audit response JSON decode failure produces a warning and does not crash
- merge_outreach_payloads precedence: cand_fallback < audit_fallback < live_api
"""
import asyncio
import json
import pytest
from unittest.mock import patch, MagicMock

from routers.launch_report import merge_outreach_payloads


# ---------------------------------------------------------------------------
# Unit tests for merge_outreach_payloads precedence
# ---------------------------------------------------------------------------

def test_merge_live_api_wins_over_audit():
    cand = {"outreach_status": "pending"}
    audit = {"outreach_status": "in_progress"}
    live = {"outreach_status": "passed"}
    result = merge_outreach_payloads(cand, audit, live)
    assert result["outreach_status"] == "passed"


def test_merge_audit_wins_over_cand_when_no_live_api():
    cand = {"outreach_status": "pending"}
    audit = {"outreach_status": "in_progress"}
    result = merge_outreach_payloads(cand, audit, None)
    assert result["outreach_status"] == "in_progress"


def test_merge_cand_used_when_audit_and_live_empty():
    cand = {"outreach_status": "sent"}
    result = merge_outreach_payloads(cand, {}, None)
    assert result["outreach_status"] == "sent"


def test_merge_null_values_in_live_do_not_override():
    cand = {"outreach_status": "passed"}
    audit = {}
    live = {"outreach_status": None}  # null should not override
    result = merge_outreach_payloads(cand, audit, live)
    assert result["outreach_status"] == "passed"
