# JobDiva provisioning: the profile-identity invariant and its fail-safes

**Rule.** A person sourced from JobDiva (TalentSearch, JobAgent, Applicants) must
**never** get a second JobDiva profile from PAIR. Launch PAIR attaches the job
application to the profile JobDiva already has.

## Why this went wrong before (2026-06 → 2026-09)

`POST /apiv2/jobdiva/CreateJobApplicationWithResume` has **no `candidateid`
field**. Its body schema (`UploadResumeAndApplyJob`) is exactly `filecontent,
filename, jobid, recruiterid, resumeDate, resumesource, textfile`. JobDiva
silently ignores undefined fields, so the id the provisioner "linked" was dropped,
JobDiva parsed the résumé text into a **new** profile every time, returned the new
id, and the provisioner persisted that duplicate's id over the real one. Two
earlier fixes (PR #493, PR #520) changed *which* id was passed; neither could work.

The same silent-ignore behaviour produced the TalentSearch "wrapped payload"
bug (`scripts/jobdiva_payload_variants_probe.py`). Treat every JobDiva request
body as schema-exact.

## The two endpoints

| Purpose | Endpoint | Body | Creates a profile? |
|---|---|---|---|
| Attach an **existing** profile to a job | `POST /apiv2/jobdiva/createJobApplication` | `{candidateid, jobid}` (+ optional `dateapplied`, `globalid`, `resumesource`) → boolean | **No** |
| Create a profile from résumé text **and** apply | `POST /apiv2/jobdiva/CreateJobApplicationWithResume` | `UploadResumeAndApplyJob` (no `candidateid`) → int64 new profile id | **Yes** |

Code: `services/jobdiva.py` `link_candidate_to_job` (attach) and
`create_job_application_with_resume` (link-first; creates only when nobody matches).

## Fail-safe layers

1. **Link-first service.** `create_job_application_with_resume` attaches when a
   profile id is given or found via `searchCandidateProfile`. A failed attach is
   retried only with a *different* looked-up id; it never falls back to creating.
2. **Kill-switch.** The provisioner passes `allow_profile_creation=False` for every
   row it could resolve a profile for. The service refuses the create path outright
   and logs `JOBDIVA_PROFILE_INVARIANT`.
3. **Fail-closed guard.** In `routers/engagement.py` `_provision_one`, a
   JobDiva-sourced row with no resolvable link id is *not* provisioned; it is
   logged as `jobdiva_row_without_link_id` and counted as failed.
4. **Id-drift tripwire.** If JobDiva returns a profile id that is not the
   JobDiva-sourced person's own id, the row keeps its real id, the event is logged
   as `unexpected_profile_id`, and `_provision_batch_to_jobdiva` reports it under
   `duplicate_suspected` (also surfaced by `/engage/re-provision`).
5. **Label-independent provenance.** Every JobDiva pool emitter stamps
   `jobdiva_candidate_id` = its own id (`jobdiva_profile_stamp`); the frontend passes
   it through on `/candidates/save`; `jobdiva_profile_id` trusts a numeric
   `candidate_id` when *either* the `source` label starts with `JobDiva` *or* the
   stamp equals the id. Renaming a source label can no longer turn JobDiva people
   into "unknown" people.
6. **Own id beats stored id.** `_resolve_link_candidate_id` prefers a JobDiva row's
   own `candidate_id` over a stored `jobdiva_candidate_id` that disagrees (that stored
   value is the duplicate the old code persisted) and logs the pair.
7. **Schema contract tests.** `tests/fixtures/jobdiva_swagger_v2_endpoints.json`
   is a snapshot of JobDiva's Swagger for every endpoint we call.
   `tests/test_jobdiva_payload_contract.py` fails when a payload we build uses a
   field the schema does not define. Refresh / drift-check with
   `python scripts/jobdiva_swagger_snapshot.py [--check]`.
8. **Regression tests.** `tests/test_jobdiva_link_via_create_job_application.py`
   (service), `tests/test_jobdiva_provisioner_failsafes.py` (provisioner),
   `tests/test_jobdiva_source_labels.py` (emitted labels and stamps).

## Detecting and repairing (admin)

- `GET /api/v1/engagement/engage/jobdiva-profile-audit?job_id=<optional>` lists
  JobDiva-sourced rows whose stored id is not their own id: `real_profile_id` vs
  `duplicate_profile_id`. Merge the pair inside JobDiva.
- `POST /api/v1/engagement/engage/jobdiva-profile-audit/repair` `{ "job_id": <optional> }`
  re-stamps `jobdiva_candidate_id` = `candidate_id` on those rows (idempotent; DB only).
- Alert on the log marker `JOBDIVA_PROFILE_INVARIANT` or the New Relic custom event
  `JobDivaProfileInvariant`.

## When adding a JobDiva call

1. Add the path to `ENDPOINTS` in `scripts/jobdiva_swagger_snapshot.py`, refresh the fixture.
2. Build the payload from the fixture's field list; add a case to the contract test.
3. Never pass an id to an endpoint whose schema does not define it and expect linking.
