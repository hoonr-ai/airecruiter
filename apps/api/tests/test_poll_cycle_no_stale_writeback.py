"""The 5-min JobDiva status poll (main._run_poll_cycle) and the legacy
"monitored jobs file" writers must never write a stale snapshot of every job
back: that reverted the dashboard counters (candidates_launched, pair_submits,
feedback_completed, time_to_first_pass), processing_status and
recruiter_emails written while the poll waited on JobDiva."""
import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def _function(name: str) -> ast.AST:
    tree = ast.parse(MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in main.py")


def _calls(node: ast.AST) -> set:
    return {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(node) if isinstance(n, ast.Call)
    }


def test_the_poll_cycle_writes_only_the_jobs_it_changed():
    assert "save_monitored_jobs" not in _calls(_function("_run_poll_cycle"))
    assert "monitor_job_locally" in _calls(_function("_run_poll_cycle"))


def test_save_monitored_jobs_writes_only_the_legacy_status_fields(monkeypatch):
    src = ast.get_source_segment(MAIN.read_text(), _function("save_monitored_jobs"))
    namespace = {"Dict": dict, "Any": object, "_LEGACY_SNAPSHOT_FIELDS": ("status", "customer_name", "title")}
    written = []

    class _Svc:
        def monitor_job_locally(self, jid, data):
            written.append((jid, data))

    namespace["jobdiva_service"] = _Svc()
    exec(src, namespace)  # noqa: S102 — the function under test, isolated from main's imports
    namespace["save_monitored_jobs"]({"jobs": {
        "1": {"status": "OPEN", "title": "T", "candidates_launched": 0, "pair_submits": 0,
              "processing_status": "step_1_complete"},
        "2": {"candidates_launched": 9},
    }})
    assert written == [("1", {"status": "OPEN", "title": "T"})]
