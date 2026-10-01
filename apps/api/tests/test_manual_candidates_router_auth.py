"""Auth guards on the manual_candidates router.

No global auth middleware in this app: every endpoint carries its own
`Depends(get_current_user)` and job-scoped endpoints call
`_verify_job_access_by_id`. Both routes here (`/jobs/{id}/manual-candidate`,
`/jobs/{id}/bulk-resumes`) write candidates into sourced_candidates for an
arbitrary job and shipped with neither guard — an unauthenticated write path
on PROD. These tests read the router source with `ast` (no DB, no
TestClient) so a future route added without the guards fails loudly.
"""
import ast
from pathlib import Path
from typing import Dict, List

ROUTER_PATH = Path(__file__).resolve().parents[1] / "routers" / "manual_candidates.py"


def _routes() -> List[Dict]:
    src = ROUTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: List[Dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        paths = [
            d.args[0].value
            for d in node.decorator_list
            if isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "router"
            and d.args
            and isinstance(d.args[0], ast.Constant)
        ]
        if not paths:
            continue
        body = ast.get_source_segment(src, node) or ""
        signature = body.split("):", 1)[0]
        found.append({"path": paths[0], "name": node.name, "signature": signature, "body": body})
    return found


def test_router_has_the_expected_routes():
    paths = sorted(r["path"] for r in _routes())
    assert paths == ["/jobs/{job_id}/bulk-resumes", "/jobs/{job_id}/manual-candidate"]


def test_every_route_requires_an_authenticated_user():
    for r in _routes():
        assert "Depends(get_current_user)" in r["signature"], (
            f"{r['path']} ({r['name']}) has no `user: UserIdentity = Depends(get_current_user)`"
        )


def test_every_job_scoped_route_verifies_job_access():
    for r in _routes():
        if "{job_id" not in r["path"]:
            continue
        assert "_verify_job_access_by_id(job_id, user)" in r["body"], (
            f"{r['path']} ({r['name']}) never calls _verify_job_access_by_id(job_id, user)"
        )
        # The check must run before any DB work — i.e. before the first `try:`.
        before_try = r["body"].split("try:", 1)[0]
        assert "_verify_job_access_by_id(job_id, user)" in before_try, (
            f"{r['path']} verifies job access only after starting work"
        )
