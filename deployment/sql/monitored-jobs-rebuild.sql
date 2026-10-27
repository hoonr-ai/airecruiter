-- One-time rebuild of monitored_jobs (32 MB for ~2,400 rows: almost all dead
-- space left by the per-cycle rewrites).
--
-- Only AFTER the churn fixes are live (metrics refresh skipping no-op
-- updates, vacuum_truncate off) — otherwise it bloats straight back.
-- VACUUM FULL holds an ACCESS EXCLUSIVE lock for its whole run: every read
-- and write on the table waits. At this size that is seconds, but run it in
-- a quiet window (Indian night) and with psql, not through the app.
-- pg_repack does the same without the long lock, but needs its extension and
-- client installed; for 32 MB the short lock is the simpler choice.

-- 1. Before: size and dead rows.
SELECT pg_size_pretty(pg_total_relation_size('monitored_jobs')) AS total_size,
       n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
  FROM pg_stat_user_tables
 WHERE relname = 'monitored_jobs';

-- 2. Rebuild. Gives up (instead of queueing every other query behind it) if
-- it can't get the lock within 5s; just run it again.
SET lock_timeout = '5s';
VACUUM (FULL, ANALYZE, VERBOSE) monitored_jobs;
RESET lock_timeout;

-- 3. After: n_dead_tup 0 and a total of a few MB (live rows plus indexes).
SELECT pg_size_pretty(pg_total_relation_size('monitored_jobs')) AS total_size,
       n_live_tup, n_dead_tup
  FROM pg_stat_user_tables
 WHERE relname = 'monitored_jobs';
