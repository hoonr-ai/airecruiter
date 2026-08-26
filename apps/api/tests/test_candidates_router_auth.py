"""Auth guards on the candidates router.

This app has NO global auth middleware — every endpoint carries its own
`Depends(get_current_user)`, and job-scoped endpoints additionally call
`_verify_job_access_by_id`. That makes "someone added a route and forgot
the guard" a silent, recurring PROD hole rather than a loud failure, so
these tests read the router's own source with `ast` and assert the guards
are present. No DB, no TestClient.

`save_candidate_feedback` shipped without either guard: unauthenticated
callers could write JobDiva candidate notes stamped with the PAIR
recruiter id and mutate `sourced_candidates.data`, which drives the
dashboard's FEEDBACK COMPLETED and PAIR SUBMITS columns.

The rest of the router is pinned by KNOWN_UNAUTHENTICATED below — an
explicit, shrinking inventory of the routes that still have no guard.
Adding a new route without a guard fails the test; fixing one of the
listed routes also fails it, so the list has to be kept honest.
"""
import ast
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

ROUTER_PATH = Path(__file__).resolve().parents[1] / "routers" / "candidates.py"


def _routes() -> List[Dict]:
    """Every `@router.<method>("<path>")` in candidates.py with its guards."""
    src = ROUTER_PATH.read_text()
    tree = ast.parse(src)
    found: List[Dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [
            (d.func.attr.upper(), d.args[0].value)
            for d in node.decorator_list
            if isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "router"
            and d.args
            and isinstance(d.args[0], ast.Constant)
        ]
        if not decorators:
            continue
        body = ast.get_source_segment(src, node) or ""
        # The signature ends at the first "):" — everything before it is
        # where a Depends(...) default would live.
        signature = body[: body.find("):")]
        for method, path in decorators:
            found.append(
                {
                    "method": method,
                    "path": path,
                    "func": node.name,
                    "line": node.lineno,
                    "authenticated": "get_current_user" in signature,
                    "job_scoped_check": "_verify_job_access_by_id" in body,
                }
            )
    return found


def _by_func(name: str) -> Dict:
    matches = [r for r in _routes() if r["func"] == name]
    assert matches, f"route handler {name} not found in {ROUTER_PATH.name}"
    return matches[0]


# --------------------------------------------------------------------------
# The endpoint this test file was written for
# --------------------------------------------------------------------------
def test_save_candidate_feedback_requires_authentication():
    """It writes to JobDiva and to sourced_candidates — never anonymous."""
    route = _by_func("save_candidate_feedback")
    assert route["authenticated"], (
        "save_candidate_feedback must take `user: UserIdentity = "
        "Depends(get_current_user)`; without it anyone on the internet can "
        "create JobDiva candidate notes as the PAIR recruiter"
    )


def test_save_candidate_feedback_verifies_job_access():
    """Authentication alone is not enough — it must be *this* user's job.

    Uses the same guard as `GET /jobs/{id}/candidates` (the rank list this
    action is taken from), so it grants exactly the set of users who can
    already see the candidate.
    """
    route = _by_func("save_candidate_feedback")
    assert route["job_scoped_check"], (
        "save_candidate_feedback must call _verify_job_access_by_id(...) so a "
        "recruiter can't submit or reject candidates on someone else's job"
    )


def test_feedback_route_shape_is_unchanged():
    """The nginx allowlist matches on the `/candidates` path segment.

    nginx-app-locations.conf routes `^/jobs/([^/]+)/(...|candidates|...)(/.*)?$`
    to the API. If this route ever moves out from under `/jobs/{id}/candidates/`
    it needs a matching nginx change or it 404s as a Next.js HTML page.
    """
    route = _by_func("save_candidate_feedback")
    assert route["path"] == "/jobs/{job_id_or_ref}/candidates/{candidate_id:path}/feedback"
    assert route["method"] == "POST"


# --------------------------------------------------------------------------
# Inventory of routes still reachable without credentials.
# --------------------------------------------------------------------------
# Now empty: every route in this router carries `get_current_user`. Keep it
# that way — an entry here is a documented hole, not a parking space. If a
# route genuinely must be public (an unauthenticated health check, a
# provider webhook that authenticates by signature instead), add it here
# with a comment saying why, so the exception is reviewable.
KNOWN_UNAUTHENTICATED: Set[Tuple[str, str]] = set()


def test_no_unauthenticated_routes():
    """Fails on any route added without a guard, and on any list drift."""
    actual = {(r["method"], r["path"]) for r in _routes() if not r["authenticated"]}

    unguarded = actual - KNOWN_UNAUTHENTICATED
    assert not unguarded, (
        "Unauthenticated route(s) in candidates.py: "
        f"{sorted(unguarded)}. This app has no global auth middleware — "
        "add `user: UserIdentity = Depends(get_current_user)` (plus "
        "_verify_job_access_by_id for job-scoped routes)."
    )

    stale = KNOWN_UNAUTHENTICATED - actual
    assert not stale, (
        f"Route(s) now guarded: {sorted(stale)}. Remove them from "
        "KNOWN_UNAUTHENTICATED so the inventory stays accurate."
    )


def test_every_route_is_authenticated():
    """The whole router, stated positively — easier to read in a failure."""
    unguarded = [
        f"{r['method']} {r['path']} ({r['func']}:{r['line']})"
        for r in _routes()
        if not r["authenticated"] and (r["method"], r["path"]) not in KNOWN_UNAUTHENTICATED
    ]
    assert not unguarded, "Routes missing get_current_user:\n  " + "\n  ".join(unguarded)


def test_delegating_handlers_forward_the_user():
    """A handler calling another handler directly must pass `user` through.

    `update_candidate_phone_body` and `update_candidate_email_body` call
    their path-param twins as plain Python functions, not through FastAPI.
    A `Depends(...)` default is only resolved by the framework, so omitting
    the argument would bind the literal `Depends` object as `user` and
    `verify_job_access` would then read attributes off it.
    """
    src = ROUTER_PATH.read_text()
    tree = ast.parse(src)
    guarded = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "router"
            for d in n.decorator_list
        )
        and "get_current_user" in (ast.get_source_segment(src, n) or "").split("):")[0]
    }

    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in guarded:
            continue
        forwards = any(a.id == "user" for a in node.args if isinstance(a, ast.Name)) or any(
            k.arg == "user" for k in node.keywords
        )
        if not forwards:
            bad.append(f"line {node.lineno}: {node.func.id}() called without `user`")
    assert not bad, "Direct calls to guarded handlers must forward `user`:\n  " + "\n  ".join(bad)


@pytest.mark.parametrize(
    "func",
    ["get_job_candidates", "get_launched_candidate_keys", "save_candidate_feedback"],
)
def test_job_scoped_routes_check_job_access(func):
    """Every route keyed by a job id must scope to that job, not just to login."""
    route = _by_func(func)
    assert route["authenticated"] and route["job_scoped_check"], (
        f"{func} is job-scoped, so it needs both get_current_user and "
        "_verify_job_access_by_id"
    )
