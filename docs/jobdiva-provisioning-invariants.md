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

## Provenance: origin vs JobDiva linkage (2026-09)

**Rule.** `sourced_candidates.source` is the **origin channel** -- where PAIR found
the person for this job (`LinkedIn-Exa`, `Dice`, `JobDiva-Applicants`, ...). It is
written once, on the first save, and is never changed by the provisioner, the
applicant sync or a merge. Whether the person is in JobDiva, and who put them
there, is **separate state** in `data`:

| key | scope | values |
|---|---|---|
| `jobdiva_candidate_id` | person | the JobDiva profile id |
| `jobdiva_profile_origin` | person | `pair` (Launch PAIR minted the profile) / `jobdiva` (it pre-existed) |
| `jobdiva_application_origin` | job | `pair` (Launch PAIR filed this job's application) / `organic` (the person applied in JobDiva) |
| `jobdiva_provisioned_at`, `jobdiva_provisioned_from` | job | when Launch PAIR filed it, and from which channel label |

Absent keys mean *unknown* (legacy rows). Readers must never derive origin from
JobDiva linkage: after Launch PAIR *everyone* has a `jobdiva_candidate_id`.

### Why (the "everyone became a JobDiva applicant" incident)

Both application calls (`createJobApplication`, `CreateJobApplicationWithResume`)
record a job application, so JobDiva's applicant list (`bi/JobApplicantsDetail`)
then contains every provisioned Exa/LinkedIn person, and JobDiva could not say
who filed the application (`resumesource` was hard-coded 0). The applicant sync
(`services/auto_assign_service.py`) is meant to match each applicant back to the
local row -- by the provisioner's stamp, email, phone or URL -- and update it in
place. Its lookup index was built with `ref_id, num_id = mj_row` on a
RealDictCursor row, which binds the column *names*, so the index matched nobody
and every applicant was inserted as a fresh `(job, <profile id>, 'JobDiva-Applicants')`
row -- a **twin** of the origin row -- and auto-launched again. Every reader then
favoured the JobDiva-labelled twin.

### The layers

1. **JobDiva-side marker.** Configure a "PAIR" Resume Source in JobDiva and set
   `JOBDIVA_PAIR_RESUME_SOURCE_ID` (optionally per channel via
   `JOBDIVA_PAIR_RESUME_SOURCE_IDS_BY_CHANNEL="LinkedIn-Exa:12,Dice:14"`). Both
   application calls then send it as `resumesource` (`jobdiva_pair_resume_source_id`).
   `JOBDIVA_PAIR_RESUME_SOURCE_NAMES` lists the names JobDiva may echo back.
   Unconfigured (`0`) keeps the legacy payloads.
2. **Provisioner stamps linkage, not labels.** `_persist_jobdiva_link_state` writes
   person-level keys on every row of the candidate and job-level keys only on the
   job's rows; every success branch stamps `jobdiva_application_origin = pair`,
   including the "JobDiva returned no id" branch (the service first tries to
   recover the id via `searchCandidateProfile`). `create_job_application_with_resume`
   returns a `JobDivaApplicationOutcome` (still unpacks as `(success, id)`) whose
   `.path` / `.found_via_search` drive `jobdiva_profile_origin`.
3. **Sync classifies before it writes.** `_monitored_job_ids` makes the index
   shape-agnostic; `_find_in_index` also matches the stamp, the synthetic
   `pair-<digits>@no-email.jobdiva.local` address, national phone digits and
   normalised LinkedIn URLs. A match is an UPDATE that never assigns `source`,
   keeps a real score over the bypass placeholder 0, and is not auto-launched.
   An unmatched applicant whose Resume Source / recruiter is PAIR's is skipped
   and logged under `PAIR_APPLICATION_UNLINKED`. New applicants are stamped
   `organic`. The profile table is only written for rows the sync owns.
4. **Readers keep the origin label.** `GET /jobs/{id}/launched-candidate-keys`
   returns the stored profile id so Step 5 hides the JobDiva-labelled copy of a
   launched person as "launched" instead of offering a second launch. The rank
   list folds twins into the stamped origin row (`apps/web/lib/candidateTwins.ts`)
   and shows linkage as a caption ("In JobDiva · via PAIR" / "applied directly")
   under the origin label. `GET /jobs/{id}/candidates` promotes the provenance keys.
5. **Backfill (admin).** `GET /api/v1/engagement/engage/applicant-origin-audit?job_id=`
   lists twin pairs (one origin can have several twins: the sync's Applicants row
   and a re-launch's TalentSearch/JobAgent row); `POST .../applicant-origin-audit/repair
   {job_id, dry_run}` (dry-run by default) folds the engage state of ALL of an
   origin's twins into it -- key by key, the most recently updated twin winning,
   never overwriting what the origin already has -- notes `jobdiva_twin_merged_at`
   / `jobdiva_twin_sources` / `jobdiva_twin_count`, and deletes the twins. The fold
   is a correlated subquery, not an `UPDATE ... FROM` join (which applies SET from
   one arbitrary twin when there are two). Verified against a real Postgres in
   `tests/test_applicant_origin_repair_postgres.py` (runs when `pgserver` is
   installed). Legacy rows keep `jobdiva_application_origin` unknown -- it is not
   guessed.
6. **Tests.** `tests/test_applicant_sync_provenance.py` (index, matching, blob,
   one full cycle), `tests/test_jobdiva_provisioner_failsafes.py` (stamps per
   scope), `tests/test_jobdiva_link_via_create_job_application.py` and
   `tests/test_jobdiva_payload_contract.py` (`resumesource`, outcome, id recovery),
   `tests/test_applicant_origin_audit_endpoint.py`, `apps/web/lib/candidateTwins.test.ts`.

### Still to verify live

Whether `bi/JobApplicantsDetail` returns the application's Resume Source / recruiter
(the sync reads the application-level spellings `resumeSource*`, `applicationSource`,
`recruiterId`, `submittedBy`, `createdBy`, `enteredBy`; candidate-level `SOURCE` /
`OWNERID` are deliberately ignored -- a profile PAIR minted carries them for life,
and reading them would drop that person's genuine application to another job).
If the BI row carries none of these, layer 3's "PAIR-filed but unlinked" branch
never fires and the stamp/email/phone/URL match carries the whole load -- which is
sufficient once the profile id is stamped.


## Profiles PAIR creates are never blank (2026-09-25)

**Rule.** When Launch PAIR creates a JobDiva profile (a person JobDiva does not
have -- LinkedIn-Unipile / LinkedIn-Exa / LinkedIn-DeepSearch / Dice / manual),
the profile gets a real résumé *file* and every contact / address field PAIR
knows. A row with nothing but a name is not created at all.

### What was wrong

A read-back of PAIR-created profiles (`bi/CandidatesProfileDetail` +
`bi/ResumeDetail`) showed a 0-byte `.txt` résumé (PLAINTEXT: 3 characters),
JobDiva's `Auto_…@jobdiva.com` email, and empty phone / city / state / zip /
LinkedIn / experience / education. Only the name, patched on afterwards, landed.

1. `CreateJobApplicationWithResume` reads the résumé from **`filecontent`**
   (base64 file; `.pdf` / `.docx` / `.rtf` / `.txt`). Its own docs call
   `textfile` the "Alternate text resume". PAIR sent `filecontent: ""`.
2. For LinkedIn rows the text itself was `NAME / Email | Phone / (Profile
   sourced via PAIR)`: their profile is structured data, never `resume_text`.
3. The post-create phone went in a flat `phone` field that
   `UpdateCandidateProfileDef` does not define (it has `phones[]`), so no phone
   ever reached a PAIR-created profile; no address was ever sent.

### How it works now

| Step | Where |
|---|---|
| Sourcing keeps the whole LinkedIn profile (role descriptions, summary, education, skills, languages, projects, volunteering) as `linkedin_profile`; deep-search rows keep title / location / recent roles (never the agent's fit rationale). The save payload carries it into `sourced_candidates.data.linkedin_profile`. | `services/profile_resume.py` `normalize_linkedin_profile` / `deep_search_profile`; `unified_candidate_search.py`; `routers/candidates.py` save; `apps/web/app/jobs/new/page.tsx` |
| The provisioner renders the résumé: the row's own résumé when it is real (JobDiva / Dice / manual), else a LinkedIn-style résumé from `linkedin_profile` → stored `company_experience` / `education` / `certifications` / `skills` → the LLM extraction → Exa's cleaned crawl text. | `build_profile_resume` |
| Thin résumé (no work history, summary or résumé text) → one LinkedIn re-read through Unipile first. Capped per batch. | `routers/engagement.py` `_provisioning_resume`; `JOBDIVA_PROVISION_LINKEDIN_REFETCH` (default on), `JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP` (default 40) |
| No usable name, or nothing but a name → not created. Logged as `JOBDIVA_BLANK_PROFILE_REFUSED` and counted in `blank_profile_refused` (also in `failed`, so `/engage/re-provision` retries it). | `_provision_one` |
| The résumé is uploaded as a `.docx` in `filecontent` (`.txt` when python-docx is unavailable); a 4xx rejection of the document is retried once as `.txt`. A 5xx is **not** retried (the profile may exist; a second upload would duplicate it). | `create_job_application_with_resume` |
| The created profile is read back and only its blank / placeholder fields are filled: name, email, `phones: [{phone, type: "C", action: 1}]`, city / state / zipCode / countryid. A read-back that shows an OLD `DATECREATED` means JobDiva matched the résumé to a profile it already had: nothing it holds is overwritten, and the outcome's `matched_existing` keeps `jobdiva_profile_origin` = `jobdiva`. | `_complete_created_profile`, `created_profile_fill` |

The synthetic `pair-<digits>@no-email.jobdiva.local` lookup address still lands
on the profile (it is the re-provision key) but never on the résumé.

### Still to verify live

`phones[]` with type `C` / action `1` and `countryid` as an ISO-2 code follow
JobDiva's documented PhoneType / profile fields but have not been checked
against a live profile (no test profiles were created in the production
tenant). After the first QA launch, check the per-candidate log line
`🧾 JobDiva profile <id>: read_back=yes fresh=True resumes=1 parsed=[...]
filling=[...]` and open one created profile in JobDiva: résumé attached, phone,
city/state present. If JobDiva rejects a field, `_update_candidate_profile`
retries with fewer fields (address, then phones, then email) so the name always
lands -- the log shows which attempt succeeded.

Tests: `tests/test_profile_resume.py`, `tests/test_linkedin_profile_capture.py`,
and the blank-profile sections of `tests/test_jobdiva_link_via_create_job_application.py`,
`tests/test_jobdiva_provisioner_failsafes.py`, `tests/test_jobdiva_payload_contract.py`.
