"""POST /candidates/save: the first-launch stamps on monitored_jobs.

`pair_launched_at` (HC status, "Launched (PAIR)", the Time to First Pass
baseline) predates `pair_launched_by`, which a startup ALTER adds and can skip
for a boot. The attribution therefore runs as its own best-effort statement
(services/job_attribution.stamp_job_launched_by, which tolerates the missing
column and follows each attempt until the job's first successful launch)
BEFORE the launch-time UPDATE, and the launch-time UPDATE never names the
optional column, so a boot without it costs the attribution only. tests/test_report_foundations.py drives
the helper against Postgres; this pins the caller.
"""

import ast
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "routers" / "candidates.py").read_text()


def _save_candidates_body() -> str:
    tree = ast.parse(SRC)
    return next(
        ast.get_source_segment(SRC, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "save_candidates"
    )


def test_launched_by_is_stamped_on_its_own_before_the_launch_time():
    body = _save_candidates_body()
    stamp_at = body.index("stamp_job_launched_by(request.jobdiva_id, user.email)")
    launch_at = body.index("SET pair_launched_at = COALESCE(pair_launched_at, NOW())")
    assert stamp_at < launch_at


def test_launch_time_update_never_names_the_optional_column():
    body = _save_candidates_body()
    launch_at = body.index("SET pair_launched_at = COALESCE(pair_launched_at, NOW())")
    statement = body[body.rindex("UPDATE monitored_jobs", 0, launch_at):body.index('"""', launch_at)]
    assert "pair_launched_by" not in statement
    assert "LAUNCHED_BY_SET_SQL" not in SRC
