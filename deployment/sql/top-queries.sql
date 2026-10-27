-- Slowest queries by total time, from pg_stat_statements
-- (enabled by deployment/postgres-tuning.sh). To start a fresh window after
-- a fix ships: SELECT pg_stat_statements_reset();
SELECT round(total_exec_time::numeric / 1000, 1)        AS total_s,
       calls,
       round(mean_exec_time::numeric, 1)                AS mean_ms,
       round(max_exec_time::numeric, 1)                 AS max_ms,
       rows,
       round(100 * shared_blks_hit::numeric
             / nullif(shared_blks_hit + shared_blks_read, 0), 1) AS cache_hit_pct,
       left(regexp_replace(query, '\s+', ' ', 'g'), 300) AS query
  FROM pg_stat_statements
 WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
 ORDER BY total_exec_time DESC
 LIMIT 25;

-- The same, by slowest single call (what users feel as a hang).
SELECT round(max_exec_time::numeric, 1)                 AS max_ms,
       round(mean_exec_time::numeric, 1)                AS mean_ms,
       calls,
       left(regexp_replace(query, '\s+', ' ', 'g'), 300) AS query
  FROM pg_stat_statements
 WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
 ORDER BY max_exec_time DESC
 LIMIT 15;
