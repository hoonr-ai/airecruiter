# New Relic: PAIR runbook

Account **7594453** (US region; NerdGraph `https://api.newrelic.com/graphql`). The same account also holds
Pairbot (`pairbot-prod`, `pairbot-qa`) and the Node product `hoonr-api`, so always filter by `appName` /
`entity.name` / `env`.

| What | QA | PROD | local |
|---|---|---|---|
| API (APM, Python) | `PAIR API (QA)` | `PAIR API (PROD)` | `PAIR API (local)` |
| Web (Browser, copy/paste loader) | `PAIR Web (QA)` appID 1589288243 | `PAIR Web (PROD)` appID 1589288242 | `PAIR Web (local)` appID 1589288244 (opt-in) |
| `env` attribute / label | `qa` | `prod` | `local` |

Browser apps: `accountID = trustKey = 7594453`, `agentID = applicationID`, browser licence key
`NRBR-e39b9a2d35dd56ef8ea` (a public browser key, not the ingest key). The loader and its per-host table live in
`apps/web/public/newrelic-browser-agent.js` / `apps/web/lib/nr-config.ts`.

> **Never rename browser app 1549492374 ("pairbot-prod").** It is the browser app linked to Pairbot's PROD APM
> app. PAIR PROD reported into it from #524 until this change, so its history mixes both products. Renaming it
> renames Pairbot's entity. PAIR PROD now reports into 1589288242; leave 1549492374 to Pairbot.

---

## 1. What is instrumented (API)

| Signal | Where it comes from | Notes |
|---|---|---|
| Web transactions, per route | The agent's own uvicorn / Starlette / FastAPI hooks | No manual `ASGIApplicationWrapper` in `main.py`: that made uvicorn pick ASGI2 and 500 every request (#388). `tests/test_newrelic_agent.py` loads a stand-in app through `uvicorn.Config(...).load()` and asserts `asgi3` + a 200, with the agent on and off. |
| `request_id`, `job_id` on every web transaction | `core/logging.RequestIDMiddleware` | `request_id` = the `X-Request-ID` response header, so a browser report or a journald line pivots straight to the trace. |
| Background transactions `OtherTransaction/Task/scheduler/<job>` | `core.newrelic.background_transaction` in `main.py` | `job_status_poll`, `always_on_sync` (incl. the delayed first sync), `jobdiva_bi_sync`, `backfill_monitored_jobs_counters`. 7 of 8 workers skip each cycle on the advisory lock within ms: read `max`/`p95`; the count per `worker_pid` is a scheduler heartbeat (every background transaction carries `worker_pid`; all 8 workers share one host). |
| Background transactions for spawned work | `core.tasks.spawn(...)` | Named after the spawn name up to `:` (`launch`, `intake_prewarm`, ...); the suffix is the `task_key` attribute. Detached from the request that spawned it (`spawn` drops the inherited trace first, `nr_txn` or not). `active_job_refresh` keepalive loops get no transaction. The launch-session runner passes `nr_txn=False` and opens one transaction per stage. A `background_transaction` reached while a transaction is live, in the same task or one it was created from (a `StreamingResponse` generator, Starlette's middleware task group, `gather`), is a span of that transaction; a raw `asyncio.create_task` from a request therefore stays in the request: use `spawn` for fire-and-forget work. |
| Postgres spans inside `asyncio.to_thread` | `core/newrelic._install_to_thread_propagation` | The callable runs under the caller's trace (`context_wrapper`). Kill switch `NEW_RELIC_PROPAGATE_TO_THREAD=off`. |
| Route names + spans under uvloop | `core/newrelic._instrument_running_loop` | `uvicorn main:app` picks uvloop when installed and creates the loop **before** `main` (and so `init()`) is imported. The agent wraps a uvloop loop's `create_task` only at loop creation, so every task a request spawned (BaseHTTPMiddleware runs the route in one) lost the transaction: all web transactions were named `starlette.middleware.base:BaseHTTPMiddleware`, with no DB/HTTP spans. `init()` now wraps the running loop with the agent's own helper. Regression test: `test_route_named_when_uvloop_started_before_init`. |
| Monitored-jobs cache warmer | metric `Custom/Scheduler/monitored_jobs_cache_warmer/ms` (+ `/errors`) | Deliberately **not** a transaction: every 25 s × 8 workers would be ~28k transactions/day/env. |
| JobDiva calls | `services/jobdiva.py` httpx hook | Metrics `Custom/JobDiva/<path>/ms` and `Custom/JobDiva/<path>/status/<code>` per call; a `JobDivaAPI` event only when `status >= 400` or `elapsed >= 5 s`. Never the URL, query string or body. |
| Custom events | `core.newrelic.record_custom_event` | Always carry `env`; inside a transaction also `trace.id` / `span.id`. See the table below. |
| Logs | `NewRelicLogForwardingHandler` attached by `core/newrelic.init()` | Level: PROD `WARNING`, QA/local `INFO` (`NEW_RELIC_LOG_FORWARD_LEVEL`, `OFF`). Same request_id filter and warning sampler as journald, plus a PII-scrubbing formatter. Only `request_id` is kept from record extras (`context.request_id`). A `WARNING` handler also sits on the `uvicorn` logger, so "Exception in ASGI application" / "Application startup failed" arrive. The agent's own logging hook is disabled in `newrelic.ini`. |
| Errors | agent + `core.newrelic.capture_exception` | `404 405 422` ignored; `400-403 409 429` are *expected* (visible, not counted in the error rate). |
| Deploy markers | `deploy.yml` "New Relic deployment marker" step | Change Tracking via NerdGraph, when the `NEW_RELIC_API_KEY` secret is set. |

### Custom event types

| Event | Emitted by | Attributes (no PII) |
|---|---|---|
| `PipelineStage` | `core/pipeline_trace.span` | `run_id`, `job_id`, `stage`, `ms`, `ok`, `started_at_ms`, stage counters (`batch_idx`, `n`, `scope`, `source`...). A launch session adopts its id as `run_id`. |
| `PairLaunchSession` | launch-session runner (every stage transition) | `session_id`, `job_id`, `stage`, `status`, `ms_in_stage`, `attempt`, counts |
| `PairLaunchTiming` | `services/launch_timing.record_timing_event` (session completed) | `job_id`, `session_id`, `trigger`, `mode`, `status`, `attempt`, `added_to_click_s`, `click_to_launch_s`, `machine_s`, `click_to_first_dial_s`, `sourcing_s`, `selection_s`, `enrichment_s`, `human_wait_s`, `saving_s`, `pairbot_s`, `launch_total_s`, counts |
| `JobDivaAPI` | JobDiva hook (errors / slow only) | `endpoint` (path), `method`, `status_code`, `elapsed_ms`, `response_size_bytes`, `error_kind` (≤ 80 chars, dropped if it holds `@` or a 7+ digit run) |
| `JobDivaBiLimiter` | `services/jobdiva_rate_limit` | `outcome` (`granted` / `slot_timeout` / `http_429` / `counters`), `label`, `priority`, `bi_wait_ms`, `bi_cooldown_ms`, `bi_429_total`. (`JobDivaBILimiter` with a capital I is retired: event types are case-sensitive.) |
| `PairBackendStep`, `BackendError`, `BackendMessage` | `core.newrelic.log_step` / `capture_exception` / `record_message` | `PairBackendStep` was `BackendStep`, which collided with Pairbot's event of the same name. |
| `RecordingsUpstreamFailure`, `JobDivaProfileInvariant`, `EngageSendBulkInterview`, `JobDivaSync`, `CandidateSearchSummary`, `EngageGeneratePayload` | routers/services | counters and ids only (`EngageGeneratePayload` no longer carries the payload). |

**PII rule** (applies to every new attribute): ids, enums, counters and durations only. No names, emails, phones,
résumé text, URLs with query strings or response bodies. Use `core.newrelic.safe_attributes()` (same policy as
`pipeline_trace._safe_attrs`) or hash: `sha256(email.lower())[:12]`. The browser sets `setUserId` to that hash.

---

## 2. Configuration

Precedence matters: the agent reads `NEW_RELIC_*` environment defaults once, at its first import, and then
applies `newrelic.ini` **on top**. So the ini must never carry `app_name`, `labels`, `log_level`, `log_file` or
`monitor_mode` (that is how QA ended up as `hoonr-api (Python)`). `core/newrelic.init()` runs in every worker after
`apps/api/.env` is loaded and sets the defaults before importing the agent.

| Variable | local | QA | PROD | Notes |
|---|---|---|---|---|
| `NEW_RELIC_LICENSE_KEY` | your ingest key, or unset | GitHub secret (env `qa`), else hand-added in `.env` | GitHub secret (env `production`) | Unset = agent off, every helper a no-op. |
| `NEW_RELIC_ENABLED` | – | – | – | `false` = kill switch (before import). Never written by deploys. Also `NEW_RELIC_MONITOR_MODE=false`. |
| `NEW_RELIC_ENVIRONMENT` | `local` (default) | `qa` (deploy.yml) | `prod` (deploy.yml) | Else `APP_ENV`/`ENVIRONMENT`/`ENV`, else `APP_BASE_URL` host (`pair.pyramidci.com` → prod, `pairqa.pyramidci.com` → qa), else `local`. Also the agent's `[newrelic:<env>]` ini selector (no such sections exist; harmless). |
| `NEW_RELIC_APP_NAME` | `PAIR API (local)` | `PAIR API (QA)` | `PAIR API (PROD)` | Default derived from the environment. |
| `NEW_RELIC_LOG_FORWARD_LEVEL` | `INFO` | `INFO` | `WARNING` | `OFF` disables log forwarding. |
| `NEW_RELIC_STARTUP_TIMEOUT` | `0` | `0` | `0` | Seconds each worker waits for the collector. 0 = never block boot; transactions in a worker's first seconds are not recorded. |
| `NEW_RELIC_PROPAGATE_TO_THREAD` | on | on | on | `off` stops the `asyncio.to_thread` patch. |
| `NEW_RELIC_LABELS` / `NEW_RELIC_LOG` / `NEW_RELIC_LOG_LEVEL` | `env:<env>;service:pair-api` / `stderr` / `warning` | same | same | Agent diagnostics go to stderr → journald (`journalctl -u airecruiter-api`). |
| `NEXT_PUBLIC_APP_ENV`, `NEXT_PUBLIC_APP_VERSION` (web `.env.local`) | – | `qa`, short sha | `prod`, short sha | Written by `deploy-azure.sh` before `npm run build` when `.env.local` exists. |

`newrelic.ini` keeps: transaction tracer (obfuscated SQL, **explain plans off**), error collector lists, log
forwarding with `context_data` include `request_id` only, `span_events.max_samples_stored = 1000`,
`application_logging.forwarding.max_samples_stored = 2000`, distributed tracing on, browser auto-instrument off,
`[import-hook:logging] enabled = false`. The agent version must be `>=12,<15` (`requirements.txt`); an older one is
switched off with an error log.

### Deploy wiring

- `deploy.yml` passes `NEW_RELIC_LICENSE_KEY` (secret), `NEW_RELIC_APP_NAME` and `NEW_RELIC_ENVIRONMENT`
  (literals) into both deploy jobs and the SSH heredoc. `deploy-azure.sh` writes each into `apps/api/.env` only when
  non-empty and different (values never printed), so an unset secret leaves a hand-added key alone.
- After `setup-ssl.sh` the job curls `http://localhost:8000/api/health` with `-f` and retries; an unhealthy API turns
  the deploy red. `deploy-azure.sh` runs the same check earlier but only warns (`::error::`), so `setup-ssl.sh` runs.
- Marker step: needs the repo/environment secret `NEW_RELIC_API_KEY` (a **User** key with NerdGraph access). It
  looks the APM entity up by exact name and calls `changeTrackingCreateDeployment`. `continue-on-error`; on the very
  first deploy the entity does not exist yet and the step just says so.
- The nodesource line no longer uses `sudo -E`, so the exported secrets are not handed to that remote script.

### Rollout

1. Ship with the **production** `NEW_RELIC_LICENSE_KEY` secret unset (PROD stays dark, behaviour unchanged).
2. Set the `qa` secret (or keep QA's hand-added key). QA now reports as **PAIR API (QA)**: a new entity. The old
   `hoonr-api (Python)` entity stops receiving data and keeps its history (decision 2026-10-11).
3. After ≥ 48 h on QA, compare ingest (query in §5). Then set the production secret and redeploy `main`.

### Renaming entities

- APM name = `NEW_RELIC_APP_NAME`. Changing it creates a **new** entity; history stays with the old one. To keep
  history, rename the entity in the NR UI first (APM → app → Settings → Application), then deploy the same name, and
  check the entity GUID did not change.
- `PAIR Web (*)` browser apps are copy/paste apps; renaming them in the UI is safe. Never 1549492374 (above).

---

## 3. Kill switches

```bash
# Agent off on a VM (survives deploys; deploy-azure.sh never writes NEW_RELIC_ENABLED)
echo NEW_RELIC_ENABLED=false >> ~/codebase/airecruiter/apps/api/.env && sudo systemctl restart airecruiter-api
# Back on: delete that line and restart.

# Keep APM, stop log forwarding / the to_thread patch
echo NEW_RELIC_LOG_FORWARD_LEVEL=OFF >> ~/codebase/airecruiter/apps/api/.env
echo NEW_RELIC_PROPAGATE_TO_THREAD=off >> ~/codebase/airecruiter/apps/api/.env
sudo systemctl restart airecruiter-api
```

Everything in `core/newrelic.py` is fail-open: if the agent is missing, old, misconfigured or the collector is
unreachable, requests are unaffected and helpers are no-ops.

---

## 4. Running locally

1. Python 3.11/3.12 venv: `pip install -r apps/api/requirements.txt` (brings `newrelic>=12,<15`).
2. In `apps/api/.env`: `NEW_RELIC_LICENSE_KEY=<ingest licence key>` and `NEW_RELIC_ENVIRONMENT=local`
   (app `PAIR API (local)`). Optional: `NEW_RELIC_LOG_FORWARD_LEVEL=WARNING`, `NEW_RELIC_STARTUP_TIMEOUT=10`.
3. Start against a scratch database (never the shared PROD DB):
   `cd apps/api && uvicorn main:app --port 8000`. stderr shows `[newrelic] agent initialized app=PAIR API (local)`.
4. Hit `/api/health` a few times, then in NR:
   `FROM Transaction SELECT count(*) WHERE appName = 'PAIR API (local)' FACET name SINCE 10 minutes ago`.
5. Browser: the local app is opt-in. Open the web app with `?nr=1` once (or set `localStorage.pair_nr_local = "1"`).
   With the API on `localhost:8000`, browser → API traces link through `traceparent`.
6. Nothing should be sent? `NEW_RELIC_DEVELOPER_MODE=true` with a dummy key: the agent runs fully, harvests go
   nowhere (this is what `tests/test_newrelic_agent.py` uses).

Tests: `cd apps/api && python -m pytest tests/test_newrelic_helpers.py tests/test_newrelic_agent.py` (the agent
cases need `newrelic` installed in that interpreter and run in subprocesses). `init()` never starts the agent
in-process under pytest (a key in `apps/api/.env` would otherwise report test traffic and instrument every later
test); `NEW_RELIC_ALLOW_UNDER_PYTEST=1` overrides that.

---

## 5. NRQL cookbook

Replace the app name / env as needed.

**Health**
```sql
-- Slowest routes (streams suppress Apdex but still show here)
FROM Transaction SELECT count(*), percentile(duration, 50, 95) WHERE appName = 'PAIR API (PROD)'
  AND transactionType = 'Web' FACET name SINCE 1 day ago LIMIT 30
-- Real errors (expected 4xx excluded)
FROM TransactionError SELECT count(*) WHERE appName = 'PAIR API (PROD)' AND error.expected IS NOT true
  FACET error.class, transactionName SINCE 1 day ago
-- Everything one request did (X-Request-ID from the browser / journald)
FROM Transaction, Log SELECT * WHERE request_id = '<rid>' OR context.request_id = '<rid>' SINCE 1 day ago
-- Scheduler heartbeat per worker; loss of signal = scheduler dead
FROM Transaction SELECT count(*), max(duration) WHERE appName = 'PAIR API (PROD)'
  AND name LIKE 'OtherTransaction/Task/scheduler/%' FACET name, worker_pid TIMESERIES 1 hour SINCE 1 day ago
FROM Metric SELECT average(newrelic.timeslice.value), max(newrelic.timeslice.value)
  WHERE appName = 'PAIR API (PROD)' AND metricTimesliceName = 'Custom/Scheduler/monitored_jobs_cache_warmer/ms' TIMESERIES
-- Postgres time by statement
FROM Span SELECT sum(duration), count(*) WHERE appName = 'PAIR API (PROD)' AND category = 'datastore'
  FACET name SINCE 1 day ago
-- Error logs
FROM Log SELECT timestamp, level, message, context.request_id WHERE entity.name = 'PAIR API (PROD)'
  AND level IN ('ERROR', 'CRITICAL') SINCE 1 hour ago
```

**JobDiva**
```sql
FROM Metric SELECT average(newrelic.timeslice.value) AS 'avg ms', count(newrelic.timeslice.value) AS calls
  WHERE appName = 'PAIR API (PROD)' AND metricTimesliceName LIKE 'Custom/JobDiva/%/ms'
  FACET metricTimesliceName SINCE 1 day ago
FROM Metric SELECT count(newrelic.timeslice.value) WHERE appName = 'PAIR API (PROD)'
  AND metricTimesliceName LIKE 'Custom/JobDiva/%/status/%' FACET metricTimesliceName SINCE 1 day ago
FROM JobDivaAPI SELECT count(*), max(elapsed_ms) WHERE env = 'prod' FACET endpoint, status_code, error_kind SINCE 1 day ago
FROM JobDivaBiLimiter SELECT count(*) WHERE env = 'prod' FACET outcome, priority SINCE 1 day ago
FROM JobDivaBiLimiter SELECT latest(bi_429_total), latest(bi_wait_ms) WHERE outcome = 'counters' FACET priority, host
```

**Launch timings and sessions**
```sql
-- Timing A (added -> click) and B (click -> first interview), minutes
FROM PairLaunchTiming SELECT median(added_to_click_s) / 60 AS 'A median min', average(added_to_click_s) / 60 AS 'A avg min',
  median(click_to_launch_s) / 60 AS 'B median min', average(click_to_launch_s) / 60 AS 'B avg min', count(*)
  WHERE env = 'prod' AND status = 'completed' SINCE 30 days ago
-- Where B goes
FROM PairLaunchTiming SELECT average(sourcing_s), average(selection_s), average(enrichment_s), average(human_wait_s),
  average(saving_s), average(pairbot_s) WHERE env = 'prod' FACET trigger SINCE 30 days ago
-- One session, stage by stage
FROM PairLaunchSession SELECT timestamp, stage, status, ms_in_stage, attempt WHERE session_id = '<uuid>' SINCE 2 days ago LIMIT MAX
-- Sessions that needed retries / failed
FROM PairLaunchSession SELECT max(attempt), latest(status) WHERE env = 'prod' FACET session_id, job_id
  SINCE 1 day ago LIMIT 50
-- Pipeline stages: p50/p95 per stage, and one run on a timeline
FROM PipelineStage SELECT percentile(ms, 50, 95), count(*) WHERE env = 'prod' FACET stage SINCE 7 days ago
FROM PipelineStage SELECT stage, started_at_ms, ms, ok WHERE run_id = '<run or session id>' SINCE 2 days ago LIMIT MAX
-- Jump from an event to its trace
FROM Span SELECT * WHERE trace.id = '<trace.id from the event>' SINCE 1 day ago
```

Launch Report "Turn Around Time" (job added → first interview) can differ from A + B: B is measured per session
from the first click that produced an interview, and historical/legacy launches have no click time.

**Browser**
```sql
FROM PageView SELECT count(*), percentile(duration, 75) WHERE appId = 1589288242 FACET pageUrl SINCE 1 day ago
FROM PageAction SELECT count(*) WHERE appName = 'PAIR Web (PROD)' FACET actionName SINCE 1 day ago
FROM JavaScriptError SELECT count(*) WHERE appName = 'PAIR Web (PROD)' FACET errorMessage SINCE 1 day ago
```

---

## 6. Ingest budget

The account ingested ~188 GB in 30 days (75.6 GB in Oct 1–11) against a 100 GB free tier, so every new byte has to
be earned. Measure before and after each rollout step:

```sql
FROM NrConsumption SELECT sum(GigabytesIngested) FACET usageMetric SINCE 1 day ago COMPARE WITH 1 week ago
SELECT bytecountestimate() / 10e8 AS 'GB' FROM Transaction, Span, Log, PipelineStage, JobDivaAPI, JobDivaBiLimiter,
  PairLaunchSession, PairLaunchTiming, BackendMessage FACET eventType() SINCE 1 day ago COMPARE WITH 1 week ago
```

What this change does to it:

- **Removed** (the bulk of QA's API ingest): a `JobDivaAPI` + `BackendMessage` event pair and an INFO log line per
  JobDiva call (~9M events / 30 d, each carrying up to 255 chars of response body), and the
  `[JobDivaBI] cycle done ... skipped` INFO line logged by 7 of 8 workers every cycle (now DEBUG).
  `pipeline_stage` INFO lines are not forwarded either (they duplicate `PipelineStage` events).
- **Added**: scheduler transactions (≈ 2k/day/env: poll 768 + always-on sync 768 + BI sync 384, plus the launch
  ticker's non-empty ticks; empty ticks are ignored), Postgres spans made visible by the `to_thread` patch (bounded
  by `span_events.max_samples_stored = 1000` per minute per worker, and by trace sampling), JobDiva metrics
  (metric timeslices, not events), and launch events (one `PairLaunchSession` per stage transition, one
  `PairLaunchTiming` per session).
- **Logs**: PROD forwards WARNING+ only; QA/local forward INFO (user decision) — set
  `NEW_RELIC_LOG_FORWARD_LEVEL=WARNING` on QA if its log volume shows up in the query above.
- Session polling endpoints call `ignore_transaction()` on success, so a polling tab costs nothing.

Levers, cheapest first: `NEW_RELIC_LOG_FORWARD_LEVEL=WARNING` (or `OFF`) on QA; lower
`span_events.max_samples_stored`; `NEW_RELIC_PROPAGATE_TO_THREAD=off`; `NEW_RELIC_ENABLED=false` on QA.
Logs forwarded at INFO can include candidate names (emails and long digit runs are masked, names are not): another
reason to keep PROD at WARNING.
