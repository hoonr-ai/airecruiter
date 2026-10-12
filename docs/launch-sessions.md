# Launch sessions: server-driven Source & Launch

Clicking **Source & Launch PAIR** in Step 5 creates one durable `launch_sessions` row that holds the full input. A worker that holds a DB lease on the session runs its stages and writes a checkpoint after each unit of work. If the API crashes, is redeployed or recycles a worker, or the recruiter closes the tab, the session is not lost: another worker adopts it and continues from its checkpoint. The browser only creates the session and watches it.

Code:

- `apps/api/services/launch_sessions/` holds the runner and the stages:
  - `schema.py`: tables and vocabulary.
  - `store.py`: all SQL.
  - `runner.py`: claims, leases and the stage loop.
  - `context.py`: the lease and the stage context.
  - `stages/`: S1–S8.
  - `effects.py`: the seam to the D1/D2/C code.
  - `control.py`, `views.py`, `models.py`: the endpoint side.
  - `rules.py`: the Step-5 rules shared with `apps/web/lib/launch-rules.ts`.
- `apps/api/routers/launch_sessions.py` holds the HTTP layer.
- Tests are `apps/api/tests/test_launch_sessions_*.py`.

## Lifecycle

### Session status

| status | meaning | who moves it |
|---|---|---|
| `collecting` | provided mode: the browser is uploading rows (≤ 50 per call) | `POST …/start` → queued; housekeeping → cancelled after 15 min idle |
| `queued` | waiting for a worker | a claim → running; housekeeping → cancelled (`cancel_reason` `queue_expired`) when never claimed `PAIR_LAUNCH_QUEUED_MAX_S` after it was queued (default 30 min with the runner off, 6 h with it on) |
| `running` | leased by a worker, or released and waiting (`phase: waiting`, `retry_at`) | the runner |
| `awaiting_input` | QA review: waiting for `POST …/confirm`. No owner and no lease, so no worker slot is held | confirm → queued; cancel; housekeeping → cancelled after 24 h |
| `completed` / `failed` / `cancelled` | terminal | — |

Only one session per job can be active (`collecting`, `queued`, `running` or `awaiting_input`). A unique partial index enforces this (`ux_launch_sessions_active_job_v2`). A session past S7 (stage `provisioning` or `done`) no longer holds its job: its launch is over (`launch_complete` in the view), so a new Source & Launch, a needs-contact child or a retry child may start while it finishes its JobDiva provisioning.

### Stages

| stage | step | what it does | resume point |
|---|---|---|---|
| `created` | — | → `sourcing` (full_search) or `selecting` (provided) | — |
| `sourcing` | S1 | runs the browser's precomputed relaxation chain through `iter_search_events` and one `rules.StreamRun`; stops early once qualified ≥ target | `checkpoint.s1.next_attempt`; finished attempts are replayed from `launch_session_stream` |
| `selecting` | S2 | `compute_auto_launch_selection` over the parity pool, using the server's "already launched" predicate | `checkpoint.s2.done` |
| `enriching` | S3 | `enrich_contacts_for_launch`: groups of 5, 20 in flight, `assert_owner` before each group | per-row `contact.done` |
| `review` | S4 | QA only: hands the session to the recruiter (`awaiting_input`) | `checkpoint.review.confirmed` |
| `splitting` | S5 | DNC re-check on the effective phone, then the ready / needs-contact split | `checkpoint.s5.done` |
| `saving` | S6 | `save_candidates` in batches of 25, `assert_owner` first | rows still `ready` |
| `launching` | S7 | session claim, then `run_launch`; settles rows against Pairbot by-job; at the end hands the claims back and fires the side effects (the launch is over) | `checkpoint.s7` + row states + engage state; `checkpoint.fx` |
| `provisioning` | S7b | `provision_sent_candidates` in chunks of 25 (respects the kill switch); a chunk that raises is retried `PAIR_LAUNCH_PROVISION_TRIES` (3) times, then counted in `counts.provision.failed` / `errors`, never failing the session | `checkpoint.s7b.next` |
| `done` | S8 | claims handed back again (idempotent), counts, timing event → `completed` | `side_effects_fired_at` guard |

A full search's search phase (`created` / `sourcing` / `selecting`) runs under `PAIR_LAUNCH_SEARCH_CONCURRENCY`; the launch phase (S3–S7) under `PAIR_LAUNCH_GLOBAL_CONCURRENCY`; S7b / S8 under no global cap (see Claims).

### Candidate row states

A row moves through these states:

- `pooled`: in the table, or uploaded.
- `dropped`: removed from the table while sourcing. The reason is `dropped`, or `merged:<survivor>` when it was merged into another row.
- `selected`
- `skipped` (with a reason; see below)
- `needs_contact`, which is parked
- `ready`
- `saved`
- `launching`: claimed by this session and being posted.
- `launched`, with one of these reasons:
  - `sent` / `reconciled` / `unrecorded`: this session created the interview.
  - `already_launched`: another path launched this person meanwhile.
- `failed`, with `failure_stage` set to `save` or `engage`. Reasons: `save_rejected` (a 4xx, or a payload the save request model refuses — only the error type is stored), `not_saved`, `launch_failed` (the batch failed before its POST), `pairbot_rejected` (Pairbot created no interview: `counts.no_interview`), `not_created` (posted, Pairbot provably never created it).
- `unconfirmed`: posted, but Pairbot could not settle it.
- `handed_off`: moved to a child session.

Skip reasons:

- From selection: `launched`, `in_flight`, `applicant_backlog`, `blocked:<engage_block_reason>`, `exclusion:<reason>`, `no_contact`, `unscored`, `below_floor`, `over_limit`, `duplicate_id`, `identity_duplicate:<survivor>`.
- From later stages: `dnc`, `hard_filter`, `client_conflict`, `review_removed`, `excluded:<core reason>`, `in_flight`, `claim_lost`, `duplicate_person_this_job`, `skipped_by_recruiter`.

## Parity decisions

- **Selection pool (parity).** S2 selects from `StreamRun.selection_pool()`. That is the de-duplicated table, or every attempt's rows when the table is shorter, which is exactly what the browser selects from. Every later stage then runs on **table** rows whose id was selected, so a selected id that was merged away launches nobody. Selecting from the table alone would use the last (relaxed) score and could launch rows the browser leaves out (D2 report: 6 of 10 random runs differ).
- **Qualified** = `StreamRun.qualified()`: every attempt's rows with `match_score ≥ 60`, counted whatever their exclusion or launched state — the fixed 60 rule, whatever the session's `min_score`.
- **Score floor** (S2): `min_score` (default 60) for a full search and for `launch_remaining` (the browser's "launch remaining" applies the 60% floor too; default cap 250); none for `qa_quick` and the child sessions; another provided session uses `min_score` only when the request gives one.
- **Search payloads are bound to the job.** `iter_search_events` does no access check of its own (it runs the job's criteria lookups, JobAgent search and cross-submission emails), so each `search_attempts[i].payload.job_id` must be the session's job id or JobDiva ref (else 422 `{code: job_mismatch}`); an absent one is set to the job id, and S1 re-binds it before every search.
- **Already launched** is server truth, never mere row existence:
  - **Launched:** the row has an interview id (an audit row, or `data.engage_interview_id`) under either job key. Its `data.jobdiva_candidate_id` twin counts as launched too.
  - **In flight:** `queued`, `processing` or `sending`.
  - **Blocked:** never-engaged rows from JobDiva applicants (the backlog policy), and rows with `engage_block_reason`.
  - **Re-selectable:** rows with `engage_status = 'failed'`, and saved-but-never-engaged rows from other sources.
- **Contacts.**
  - A lookup's contact **replaces** the row's own (`override.x || c.x`).
  - Needs-contact answers merge over the lookup's (an empty field keeps the lookup's value).
  - Contacts confirmed in the QA review are **final**: they replace both channels, and an empty field means none. QA never dials a real candidate it overrode.
- **Review** happens whenever `require_review` is set, in every mode. If the request does not say, the default is true on QA (`PAIR_LAUNCH_REQUIRE_REVIEW_DEFAULT=auto`). After `/confirm`, S5 still applies DNC and the duplicate rule.

## Durability

### Claims, leases, fencing

- **Claim.**
  - Under `pg_advisory_xact_lock(728197)`, the claim counts live leases per phase. It then claims at most the free local slots (`PAIR_LAUNCH_WORKER_CONCURRENCY`, reserved synchronously before the claim is awaited, so a tick and a POST's `claim_now` never both take the last one): launch-phase sessions up to `PAIR_LAUNCH_GLOBAL_CONCURRENCY` (3) live, full searches in their search phase up to `PAIR_LAUNCH_SEARCH_CONCURRENCY` (8) live, and sessions past S7 (provisioning / done) with no global cap (legacy's provisioning was fire-and-forget; the per-worker cap bounds them) — queued ones, or running ones whose lease has expired, once `not_before` has passed. It uses `FOR UPDATE SKIP LOCKED`.
  - Order: the launch phase, then search, then wrap-up; within a phase cancel-requested first, then child sessions (a recruiter waits on a modal), then the oldest. The view's `queue_position` is a waiting session's place in that order within its phase.
  - A full search whose S2 is done is **handed over**: the runner releases it and re-claims it at once under the launch cap (or a later tick does, when the launch cap is full) — each cap holds exactly.
  - Each claim bumps `claims` and returns `(id, claims)`.
  - Sessions with `cancel_requested` **are** claimed — first, past the caps; a cancel clears `not_before` and a release of a cancel-requested session sets none (only a failed finalize backs off, so a persistent error never spins), so the cancel is finalized at once, not after a 10-min Pairbot wait.
- **Fencing.**
  - Every runner write is scoped to `owner + claims + status='running'` (see `store.py`). Zero rows raises `OwnershipLost`.
  - The fencing UPDATE also locks the session row for the rest of the transaction, so a reclaim can never interleave with a write.
  - `assert_owner()` runs before every externally visible unit: each Pairbot POST batch (inside the core's `bulk_id_for` hook), each save batch, each paid lookup group and each provisioning chunk. It requires more than 30 s left on the lease, and renews it.
- **Lease.**
  - The lease lasts 180 s and the heartbeat runs every 30 s. A heartbeat that raises is not treated as ownership loss.
  - If no renewal has succeeded for lease − 20 s, the runner starts no new unit (self-fence).
  - A lost owner does **not** cancel an in-flight Pairbot batch. It lets the batch finish (its writes to `sourced_candidates` are facts) and writes nothing to the session.
- **Release.** An owner-scoped release sets `lease_until = now()` and clears `owner` (the status stays running), so the next tick anywhere adopts the session and a heartbeat still in flight can no longer re-lease it. Anything the claim must still write (a `waiting` / `retry_scheduled` event) is written before the release.
- **Shutdown.** `core.tasks.stopping` is set first in lifespan shutdown and checked at every unit boundary; the runner then releases. On the drain's `CancelledError`, the release is a synchronous statement. A SIGKILL is covered by lease expiry.
- **Cancel.** Seen on the claim, a heartbeat, `assert_owner` or any fenced update (they all return the flag). The hand-off to QA review never happens over a pending cancel (`to_awaiting_input` refuses it and the runner cancels instead).
- **Errors.**
  - A stage exception adds 1 to `attempt`. The session is released with `not_before = min(15 min, 30 s·2^(attempt−1))`.
  - At `PAIR_LAUNCH_MAX_ATTEMPTS` (5), the runner does a reconcile-only pass, then marks the session `failed`.
  - A 403 is retried twice, then `failed ('access revoked')`.
  - A 409 (outreach stopped) → `cancelled ('outreach stopped')`.
  - A 4xx from save — or a payload the save request model refuses (pydantic `ValidationError` / `TypeError`, the browser's 422) — fails that batch's rows; there is no retry and the next batch goes on.
  - A 429 / 5xx from save is retried 3 times inside the stage.
  - Waiting for Pairbot raises `RetryLater`: a release with `not_before` that **does not** count an attempt.

### S1 replay

- Each flush (≤ 50 events or 2 s) commits three things in one fenced transaction:
  - the raw events, to `launch_session_stream`;
  - the table rows that changed (light: no résumé text, no LinkedIn profile, until the end of S1);
  - `checkpoint.s1`: attempt, pool, qualified, source statuses, stage text, JobDiva-criteria flag, errors and detail-failed count.
- An interrupted attempt is re-run from its start (cheap because of caches, but Exa Pass A/B is paid again). Finished attempts are rebuilt by replaying their events through a fresh `StreamRun`: the same reducer gives the same state.
- Cross submissions are scheduled once per session. The flag is written before the first stream starts, so delivery is at most once.

### S7: never post a person twice

- **Session claim.** Each pass claims its rows for the session (`queued` + `engage_session_id`). Legacy launch paths treat these rows as already sent.
- **Send claim.** The D1 core moves each batch to `sending` right before its POST and posts only what it won.
- **Before each POST.** The core calls the runner's `bulk_id_for(batch_idx, posted_ids)`. That hook asserts ownership, then records `checkpoint.s7.inflight[bulk_id] = {batch, attempt, ids, posted_at}` (fenced and committed), and returns the deterministic `uuid5(NS, "<session>:<launch_attempt>:<batch_idx>")`. Pairbot honours a client bulk_id. Each new pass uses a new `launch_attempt` and so a new bulk id: re-POSTing a bulk id would reset Pairbot's creation.
- **After every batch.** The rows' engage state is re-read, because it is the ground truth. A batch that failed **before** its POST (nothing recorded in `inflight`, not our own stop) fails its rows (`launch_failed`, retryable by a retry-failed child) and hands their claims back: re-planning them would hit the same error forever. Every batch that did not complete (except our own stop) counts in `counts.failed_batches`. Three passes in one claim that leave the same rows plannable raise a counted failure (bounded by `PAIR_LAUNCH_MAX_ATTEMPTS`).
- **Hand-back** (end of S7, and again at every finish — completed, cancelled or failed). Only claims the session **never posted** are released (`release_session_claims` with the ids). Rows that may be at Pairbot without a recorded outcome — posted (`inflight`) and left `queued` by D1's no-answer path, `unconfirmed`, or `launched/unrecorded` — are **parked**: `engage_status = 'processing'` with their `engage_bulk_id`, keeping `engage_session_id`. Every launch path then reads them as in flight, so nobody posts them again; the housekeeping re-check settles them. A hand-back error fails the finish, which is retried, so a terminal status never hides a leaked claim.
- **Settling (resume).** Rows that may be at Pairbot (`processing`, `sending`, or `queued` and recorded in `inflight`) go through `reconcile_with_pairbot`:

| Pairbot by-job says | row becomes |
|---|---|
| listed under its own id (`sent`) | `launched/reconciled` (written through) |
| proven, DB write failed (`unrecorded`) | `launched/unrecorded`; housekeeping re-reconciles to record it |
| listed under another id (`duplicate_of`) | `skipped/duplicate_person_this_job`, never re-posted |
| `missing`, posted ≥ `PAIR_LAUNCH_REPOST_AFTER_S` (600 s) ago or never posted | requeued (`queued`, removed from `inflight`) → posted again in a new pass |
| `missing`, posted recently | wait (`RetryLater` until it is 10 min old) |
| bulk still creating (`bulk_in_progress`, `own_bulks_creating`) | wait |
| by-job `error` (Pairbot down) | wait; nothing is re-posted |
| still ambiguous after `PAIR_LAUNCH_SETTLE_MAX_S`, or in a reconcile-only pass | `unconfirmed`, counted, parked `processing`; housekeeping re-checks by-job for 24 h (listed → launched and provisioned to JobDiva; missing → `failed/not_created`, never re-posted) |

  `own_bulk_ids` = the recorded bulk ids, minus those posted in the last `PAIR_LAUNCH_FRESH_BULK_S` (30 s).

### Side effects (end of S7)

Side effects fire once per session, and only if this session launched someone:

- Only rows the session itself launched count: `sent` / `reconciled` / `unrecorded`.
- The guard is a conditional UPDATE of `side_effects_fired_at`, claimed before spawning. A spawn error resets it. The outcome is recorded in `checkpoint.fx` / `counts.side_effects`.
- A completed launch fires them when S7 ends — the launch is over; JobDiva provisioning (S7b) follows, as legacy's inline provisioning ran in the background.

What fires depends on how the session ended:

- **Completed:**
  - recruiter email and applicant sync (initial launch);
  - the job-posting email, only if it was requested **and** the launch was clean, as `/engage/launch` decides it: no failed launch batch (`counts.failed_batches == 0`) and nothing unconfirmed. A per-person "no interview" answer (`counts.no_interview`) is not a failed batch.
- **Cancelled / failed with ≥ 1 launched:** the recruiter email and the applicant sync (initial launch) — what `/engage/launch` fires on a 409 abort — never the job posting.

JobDiva provisioning never depends on how the session ends: a cancelled / failed session provisions whoever it launched in its finalize path, and rows the housekeeping re-check proves launched later are provisioned then (claim-stamped, safe to repeat).

A pass that launched nobody never uses the guard. Needs-contact and retry run as **child** sessions, each with its own email:

- the needs-contact child sends the recruiter email only;
- the retry-failed child sends nothing.

Behaviour change from the browser flow: the count in each email covers only that session's launches.

Timings: a child of a parent that launched nobody carries the parent's `clicked_at` (and the time the recruiter spent on the modal as `human_wait_ms`), so the job's "Launch click → Launch" and "Added → Launch click" are measured from the real click. A child of a parent that launched someone keeps its own click time (it is a relaunch for the timing event).

## API (prefix `/api/v1`, every route authenticated and access-checked)

| method & path | body | answer |
|---|---|---|
| `GET /launch-sessions/config` | — | `{enabled, poll_ms, hidden_poll_ms, max_upload_chunk, max_candidates, require_review_default}`. `enabled` = `PAIR_SERVER_LAUNCH_ENABLED` **and** `PAIR_LAUNCH_RUNNER_ENABLED`: whether a new click starts a server session. The browser reattaches to, watches and answers (review, needs-contact, retry) existing sessions whatever it says |
| `POST /jobs/{job}/launch-sessions` | `{idempotency_key, mode, trigger, search_attempts[{label,payload}], min_score, qualified_target, launch_count, require_review?, options{notify_recruiters, send_job_posting_email, is_initial_launch, job_board_ids (stored, unused), batch_size}, app_base_url, candidates[≤50], start, from_session_id, candidate_ids}` | 201 `{session}`; 200 same-key replay; 409 `{code: active_session, active_session_id}` / `{code: outreach_stopped}` / `{code: idempotency_key_in_use}`; 422 invalid / `{code: idempotency_mismatch}` / `{code: job_mismatch}`; 400/404/503 recruiter-email gate; 503 `{code: disabled}` (sessions or the runner off); 404 a `from_session_id` that is not a session of this job (malformed ids included) |
| `GET /jobs/{job}/launch-sessions?active=&needs_contact=&limit=` | — | `{sessions: [view]}` (reattach) |
| `GET /launch-sessions/{id}` | — | `{session}`: status, phase, stage, `terminal`, `launch_complete`, `queue_position`, counts, `needs_contact_count`, timings, sourcing progress (incl. `sourcing.plan` = each attempt's label + boolean string), review, `events_tail` (20), children |
| `GET /launch-sessions/{id}/events?after_seq=` | — | `{events, last_seq, status}` |
| `GET /launch-sessions/{id}/candidates?state=&view=table\|full\|failed&after_rev=&after_id=&limit=&offset=&ids=` | — | `{rows, total, cand_rev, next?, has_more}`. `ids` = comma-separated candidate ids (≤ 50, else 422): the résumé viewer reads one row with `view=full&ids=<id>&limit=1` |
| `POST /launch-sessions/{id}/candidates` | `{candidates[≤50]}` | `{added, total}`; 409 not collecting; 422 |
| `POST /launch-sessions/{id}/start` | — | `{session}`; 409 not collecting; 422 empty |
| `POST /launch-sessions/{id}/confirm` | `{contacts{cid:{phone,email}}, candidate_ids?}` | `{session}`; 409 not awaiting_input |
| `POST /launch-sessions/{id}/needs-contact` | `{contacts, candidate_ids, skip_ids, idempotency_key?}` | 201 `{session: child, skipped}`; 200 `{session: null, skipped}`; 409 parent's launch not over (`launch_complete` false) / another active session; 503 `{code: disabled}` runner off (a skip-only answer or a replay still works) |
| `POST /launch-sessions/{id}/retry-failed` | `{candidate_ids?, idempotency_key?}` | 201 `{session: child}`; 422 nothing failed; 503 `{code: disabled}` runner off |
| `POST /launch-sessions/{id}/cancel` | `{reason?}` | `{session}` (cancelled now, or `cancel_requested` while running); 409 not active |

Polling:

- **Session.** `GET` the session every `poll_ms` (3 s) while it is not terminal, and every 15 s when the tab is hidden. Back off on network errors up to 15 s, and never give up while mounted.
- **Table.** Rows come incrementally with the keyset cursor: start with `after_rev=0`, follow `next` while `has_more`, then keep polling with the last `next`. Revisions are committed in order, so a cursor never skips a later write. Never combine `state=` with the cursor (a row that leaves the filtered states is never returned again); `state=` is for one-shot reads (review / needs-contact modals, `view=failed`). `updated_after` is deprecated and best-effort (it compares transaction-start timestamps).
- **Done.** Act on `launch_complete` (results, the needs-contact modal, a new launch); `terminal` follows once JobDiva provisioning is done. Redirect rule: `counts.failed − counts.no_interview == 0` and `counts.sent + counts.already_launched > 0` and `counts.needs_contact == 0`. `counts.saved` / `saved_total` are S6's total; `rows_in_saved` the rows left saved at the end.
- **Uploads.** `POST …/candidates` has its own nginx location under `pair_batch_limit` with a 25 MB body limit. Retry a 429, honouring `Retry-After`.

## Kill switches and knobs

- `PAIR_SERVER_LAUNCH_ENABLED`: `false` = new clicks use the legacy flow (`/config` `enabled: false`, `POST create` answers 503 `{code: disabled}`). It gates CREATION only: sessions that already exist still finish, and the browser still reattaches to them, shows their progress and offers their review / needs-contact / retry modals.
- `PAIR_LAUNCH_RUNNER_ENABLED`: `false` = no worker claims anything, and creation is off too (`/config` `enabled: false`; create, needs-contact and retry-failed answer 503 `{code: disabled}`). Sessions already queued wait; housekeeping (which keeps running) cancels the never-claimed ones after `PAIR_LAUNCH_QUEUED_MAX_S` (`queue_expired`), so a job is not held for hours and an hours-old search never launches when the runner comes back. A session re-queued after it ran (a confirmed QA review) is not expired — cancel it from the UI if needed.
- **Defaults of both:** unset (or not a boolean) = **ON on QA and local, OFF on PROD**, from `core.newrelic.resolve_environment()` (`NEW_RELIC_ENVIRONMENT`, then `APP_ENV` / `ENVIRONMENT` / `ENV`, then the `APP_BASE_URL` host); if that cannot be resolved, ON unless something looks like PROD. An explicit `true` / `false` always wins. PROD needs **both** set to `true`.
- `PAIR_LAUNCH_QUEUED_MAX_S` (`0` = never): the queue expiry above. Default 1800 (30 min) while `PAIR_LAUNCH_RUNNER_ENABLED` is off, 21600 (6 h) while it is on — with the runner on a session only waits while the global caps are full, and a busy queue must not lose it. A create retried after a lost answer replays its existing session even while the runner is off.
- `JOBDIVA_PROVISIONING_ENABLED=false`: no JobDiva provisioning on any path (inline, S7b, re-provision). **Set it locally** for end-to-end runs.
- `PAIR_LAUNCH_GLOBAL_CONCURRENCY` (3, launch phase) / `PAIR_LAUNCH_SEARCH_CONCURRENCY` (8, search phase) / `PAIR_LAUNCH_WORKER_CONCURRENCY` (1).
- All other knobs are listed in `apps/api/.env.example` (`PAIR_LAUNCH_*`).

## Rollout and deploys

- **Flags from GitHub.** `PAIR_SERVER_LAUNCH_ENABLED` and `PAIR_LAUNCH_RUNNER_ENABLED` are GitHub *environment variables* (Settings → Environments → `qa` / `production` → Variables). `deploy.yml` passes them to the VM and `deploy-azure.sh` writes each into `apps/api/.env` only when it is non-empty (`upsert_env`), then restarts the API. Unset = the code default (QA on, PROD off).
  - **Turn PROD on** after QA signs off: set both to `true` in `production`, then re-run the latest `main` deploy (`gh run rerun <run-id>`) or let the next promote deploy it.
  - **Turn it off** anywhere: set `PAIR_SERVER_LAUNCH_ENABLED` (new clicks → legacy) or `PAIR_LAUNCH_RUNNER_ENABLED` (stop claiming) to `false` and re-run the deploy. Deleting a variable does not remove a value already written to `.env` — set it explicitly instead.
- **Deploy-time adoption latency (lease 180 s).** A deploy restarts the API (systemd SIGTERM, `TimeoutStopSec=30`). The runner sets its stop flag and releases its sessions in lifespan shutdown (a 25 s drain), but uvicorn reaches lifespan shutdown only after every open HTTP connection has closed — and the Step-5 search stream, the legacy `/engage/launch` SSE and the enrich NDJSON stream keep connections open, so systemd usually SIGKILLs the workers first. Until then, ticks in the dying workers can still claim sessions and start a unit (the stop flag is not set yet). The sessions those workers held are adopted only when their lease expires, **up to `PAIR_LAUNCH_LEASE_S` (180 s) after the kill**. Correctness holds (fencing; S7 never re-posts — the same path as a SIGKILL), it is latency: a running launch can pause up to ~3 min across a deploy. The systemd unit is deliberately unchanged here; the fix (`--timeout-graceful-shutdown 5` on ExecStart with a 20 s drain so 5 + 20 < 30, or a SIGTERM handler that sets the stop flag before uvicorn's) is a follow-up. Do not shorten the lease to hide it: a lease must outlast a slow Pairbot POST plus heartbeat hiccups.

## Runbook

```sql
-- One session (no input / candidate data)
SELECT id, job_id, jobdiva_ref, trigger, mode, status, stage, attempt, claims, owner,
       lease_until, not_before, cancel_requested, error, counts, checkpoint - 's1' AS ck,
       clicked_at, completed_at
FROM launch_sessions WHERE id = '<session>';

-- Active / stuck sessions
SELECT id, job_id, status, stage, owner, lease_until, not_before, attempt, updated_at
FROM launch_sessions WHERE status IN ('collecting','queued','running','awaiting_input')
ORDER BY created_at;

-- Its rows by state / reason
SELECT state, split_part(reason, ':', 1) AS kind, count(*)
FROM launch_session_candidates WHERE session_id = '<session>' GROUP BY 1, 2 ORDER BY 1, 2;

-- Its progress log
SELECT seq, payload, created_at FROM launch_session_events WHERE session_id = '<session>' ORDER BY seq;

-- What S7 posted (bulk ids → ids, posted_at)
SELECT jsonb_pretty(checkpoint->'s7'->'inflight') FROM launch_sessions WHERE id = '<session>';

-- The engage state of its rows (both job keys)
SELECT candidate_id, data->>'engage_status', data->>'engage_session_id', data->>'engage_bulk_id',
       data->>'engage_interview_id'
FROM sourced_candidates
WHERE jobdiva_id IN ('<job_id>', '<ref>') AND data->>'engage_session_id' = '<session>';
```

Interventions:

- **A session cancelled with `queue_expired`:** it was never started (the runner was off, or no worker claimed it for `PAIR_LAUNCH_QUEUED_MAX_S`). Nothing was saved or sent; the recruiter clicks again (a needs-contact / retry child expired this way keeps its rows; its parent shows them handed off, and a new Source & Launch finds those people again). Check `PAIR_LAUNCH_RUNNER_ENABLED` and the `launch_session_queue_expired` warning in the API log.
- **Unstick a session now:** `UPDATE launch_sessions SET lease_until = now(), not_before = NULL WHERE id = …`. The next tick adopts it. Never edit `owner` or `claims` by hand.
- **Cancel it:** `POST …/cancel`. A running session is finalized by the runner, including a reconcile-only pass and releasing its claims.
- **Rows left `unconfirmed`:** they are parked `processing` (every launch path skips them) and housekeeping re-checks them every 5 min for 24 h. After that they stay `unconfirmed` / `processing`. Settle them by hand against Pairbot's `GET /api/interviews/by-job/{job}`: listed → record the interview; provably absent → set `engage_status = 'failed'` and drop `engage_session_id` so a retry can take them. Never re-post without checking.

New Relic (no PII in any of these):

- `PairLaunchSession` (one per stage transition): `session_id`, `job_id`, `stage`, `status`, `ms_in_stage`, `attempt`, `claims`, `trigger`, `mode`, `count_*`.
- `PairLaunchTiming` at the end.
- `PipelineStage` with `run_id = session id`.
- Transactions are `OtherTransaction/Task/launch_session/<stage>` (`task_key` = session id), plus `scheduler/launch_session_tick` and `scheduler/launch_session_housekeeping`. Empty ticks and 2xx polls are ignored.

```
SELECT count(*) FROM PairLaunchSession FACET stage, status SINCE 1 day ago
SELECT average(ms_in_stage) FROM PairLaunchSession WHERE status = 'stage_done' FACET stage SINCE 7 days ago
```

## Retention

- Housekeeping deletes `launch_session_candidates` rows of sessions that finished more than 14 days ago and are no longer being re-checked.
- `launch_session_stream` is deleted once selection is done.
- Sessions and events are kept.
