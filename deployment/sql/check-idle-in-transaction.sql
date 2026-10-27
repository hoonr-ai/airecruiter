-- Sessions that idle_in_transaction_session_timeout = 60s would end.
-- Run a few times, at busy hours, BEFORE turning the limit on
-- (deployment/postgres-tuning.sh). Every row is a connection holding a
-- transaction open (and whatever locks it took) while doing nothing.
SELECT pid,
       usename,
       application_name,
       client_addr,
       now() - xact_start  AS in_transaction_for,
       now() - state_change AS idle_for,
       left(query, 200)    AS last_query
  FROM pg_stat_activity
 WHERE state = 'idle in transaction'
   AND now() - state_change > interval '60 seconds'
 ORDER BY idle_for DESC;
