"""monitored_jobs is set to vacuum_truncate = off once, at startup, without
taking a lock on boots where it's already set."""
from routers import jobs as jobs_router


class _Cursor:
    def __init__(self, already_set):
        self.already_set = already_set
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)

    def fetchone(self):
        return (self.already_set,)


def test_sets_vacuum_truncate_off_with_a_short_lock_wait():
    cur = _Cursor(already_set=False)
    jobs_router._ensure_monitored_jobs_no_vacuum_truncate(cur)
    alter = cur.statements.index("ALTER TABLE monitored_jobs SET (vacuum_truncate = off)")
    assert cur.statements[alter - 1] == "SET lock_timeout = '2000ms'"
    assert cur.statements[alter + 1] == "RESET lock_timeout"


def test_already_set_takes_no_lock():
    cur = _Cursor(already_set=True)
    jobs_router._ensure_monitored_jobs_no_vacuum_truncate(cur)
    assert len(cur.statements) == 1 and cur.statements[0].startswith("SELECT")


def test_a_failure_never_breaks_startup():
    class _Failing(_Cursor):
        def execute(self, sql, params=None):
            if sql.startswith("ALTER"):
                raise RuntimeError("canceling statement due to lock timeout")
            super().execute(sql, params)

    cur = _Failing(already_set=False)
    jobs_router._ensure_monitored_jobs_no_vacuum_truncate(cur)  # no raise
    assert cur.statements[-1] == "RESET lock_timeout"
