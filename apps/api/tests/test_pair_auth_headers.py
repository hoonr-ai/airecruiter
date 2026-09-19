"""Auth coverage for calls to the PAIR (pairbot) API.

Two things regressed together in the same PR: `services.pair_auth` is the
fix for the duplicated header-building snippet, and the AST checks below are
the fix for the actual bug that duplication caused — a parallel fan-out
(`asyncio.gather` / several `client.get`s fired together) where the
Authorization header was wired to only one of the sibling calls. A future
call added to one of these fan-outs without going through
`get_pair_auth_headers` and passing `headers=` would silently ship
unauthenticated, exactly like the last one did, so this reads the router
source with `ast` rather than trusting call sites to remember by hand.
"""
import ast
from pathlib import Path

import pytest

from services.pair_auth import get_pair_auth_headers

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "routers"


@pytest.fixture(autouse=True)
def _reset_missing_key_warning(monkeypatch):
    # get_pair_auth_headers only warns once per process; reset between tests
    # so each test observes its own env var state.
    import services.pair_auth as mod

    monkeypatch.setattr(mod, "_warned_missing_key", False)


# ---------------------------------------------------------------------------
# get_pair_auth_headers itself
# ---------------------------------------------------------------------------
def test_adds_bearer_token_when_key_set(monkeypatch):
    monkeypatch.setenv("PAIR_API_KEY", "pair_abc123")
    assert get_pair_auth_headers() == {"Authorization": "Bearer pair_abc123"}


def test_omits_authorization_when_key_unset(monkeypatch):
    monkeypatch.setenv("PAIR_API_KEY", "")
    headers = get_pair_auth_headers()
    assert "Authorization" not in headers


def test_strips_whitespace_from_key(monkeypatch):
    monkeypatch.setenv("PAIR_API_KEY", "  pair_abc123  ")
    assert get_pair_auth_headers()["Authorization"] == "Bearer pair_abc123"


def test_json_content_type_flag_adds_header_alongside_auth(monkeypatch):
    monkeypatch.setenv("PAIR_API_KEY", "pair_abc123")
    headers = get_pair_auth_headers(json_content_type=True)
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer pair_abc123"


def test_warns_once_when_key_missing(monkeypatch, caplog):
    monkeypatch.setenv("PAIR_API_KEY", "")
    with caplog.at_level("WARNING"):
        get_pair_auth_headers()
        get_pair_auth_headers()
    warnings = [r for r in caplog.records if "pair_api_key_unset" in r.message]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# Static coverage: every call in a PAIR fan-out carries headers=
# ---------------------------------------------------------------------------
def _calls_in_function(
    file_path: Path, func_name: str, receiver_name: str, callee_attr: str
) -> list:
    """Every `<receiver_name>.<callee_attr>(...)` call inside `func_name`."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            target = node
            break
    assert target is not None, f"{func_name} not found in {file_path}"

    calls = []
    for node in ast.walk(target):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == callee_attr
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == receiver_name
        ):
            calls.append(node)
    return calls


def test_assessment_endpoint_passes_headers_to_every_pair_call():
    """engagement.py get_assessment_data fires 4 parallel client.get(...) calls
    against pairbot; each one needs `headers=` or it goes out unauthenticated."""
    calls = _calls_in_function(
        ROUTERS_DIR / "engagement.py", "get_assessment_data", "client", "get"
    )
    assert len(calls) == 4, "expected 4 client.get(...) calls in get_assessment_data"
    for call in calls:
        keyword_names = {kw.arg for kw in call.keywords}
        assert "headers" in keyword_names, (
            f"client.get(...) at line {call.lineno} in engagement.py's "
            "get_assessment_data is missing headers= (PAIR API call would go out unauthenticated)"
        )


def test_candidate_evaluation_report_passes_headers_to_every_pair_call():
    """candidates.py get_candidate_evaluation_report fires the same 4-way
    asyncio.gather(...) of client.get(...) calls against pairbot."""
    calls = _calls_in_function(
        ROUTERS_DIR / "candidates.py", "get_candidate_evaluation_report", "client", "get"
    )
    assert len(calls) == 4, (
        "expected 4 client.get(...) calls to PAIR in get_candidate_evaluation_report"
    )
    for call in calls:
        keyword_names = {kw.arg for kw in call.keywords}
        assert "headers" in keyword_names, (
            f"client.get(...) at line {call.lineno} in candidates.py's "
            "get_candidate_evaluation_report is missing headers= (PAIR API call would go out unauthenticated)"
        )
