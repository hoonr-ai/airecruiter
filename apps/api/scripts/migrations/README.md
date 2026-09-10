# SQL migrations

This repo has no migration framework. Table creation happens through
idempotent `CREATE TABLE IF NOT EXISTS` calls run once from the FastAPI
lifespan (see `main.py` and the `_ensure_*_schema` helpers in `services/`),
and `ALTER TABLE` was deliberately pulled out of those paths because it takes
an `ACCESS EXCLUSIVE` lock and stalled concurrent readers on every request.

This directory is where the resulting "manual migrations" now live, so a schema
change is reviewable in a PR instead of existing only in someone's psql
history.

## Conventions

- **One file per change**, numbered and never edited after it ships.
- **Idempotent.** Every statement uses `IF NOT EXISTS` / `IF EXISTS` so
  re-running a file is a no-op. Applying twice must be safe.
- **`CREATE INDEX CONCURRENTLY` for any index on an existing large table**
  (`sourced_candidates`, `engage_interview_audit`, `monitored_jobs`). Plain
  `CREATE INDEX` holds a `SHARE` lock and blocks writes for the duration.
  `CONCURRENTLY` cannot run inside a transaction block, so those statements
  live in their own file section and must be run outside `BEGIN`.
- **New, empty tables** may use plain `CREATE INDEX` — there is nothing to
  lock and nothing to scan.
- **Timestamps are `TIMESTAMPTZ`** in new tables. Legacy tables use naive
  `TIMESTAMP` written via `CURRENT_TIMESTAMP`; those values are interpreted as
  UTC (`col AT TIME ZONE 'UTC'`) at the few points where old and new tables are
  compared. Schema init logs the server `TimeZone` so a non-UTC server is loud
  rather than silently shifting every comparison.

## Applying

```bash
psql "$DATABASE_URL" -f apps/api/scripts/migrations/001_global_candidate_pool.sql
```

Tables in `001` are *also* created idempotently at startup by
`services/candidate_pool_schema.py`, so a deploy that hasn't had the file
applied yet still comes up working. The file exists so the schema is
reviewable, and so index/constraint changes that must not run at startup have
a home.
