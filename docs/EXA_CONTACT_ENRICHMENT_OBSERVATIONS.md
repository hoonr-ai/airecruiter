# Exa Contact Enrichment — Live Observations & Fixes

**Date:** 2026-06-09
**Branch observed:** `claude/brave-torvalds-89b5a9` (cut from `main`)
**Environment:** local dev — `web` on `:3000`, `api` (uvicorn, single worker) on `:8000`, secrets from `apps/api/.env`
**Method:** launched a real Exa candidate search through the UI and watched `api` logs through the Step-5 launch contact-enrichment path (`POST /candidates/enrich-contact`).

---

## TL;DR

When launching candidates sourced via **Exa** (LinkedIn-URL-only profiles, no seed email), **~22% get a phone number**. The contact-enrichment chain has three providers and for *phone* all three currently fail:

1. **Apollo is out of credits** — returns `HTTP 422 "insufficient credits"` on **100%** of calls. This is the dedicated phone-by-LinkedIn-URL provider; it contributes nothing.
2. **ZoomInfo never runs** for these candidates — the launch path matches **by email only**, and Exa candidates arrive with no seed email, so the step is skipped entirely (0 log lines).
3. **Exa is the only live provider, and it is weak on phone** — it returns a work **email** reliably (~67%) but a **phone only ~⅓ of completions**, and it **times out at the 60s cap on ~⅓ of candidates** (returns nothing).

Net: a phone lands only when Exa happens to return one within 60s.

---

## Measured results (9 launch-path candidates)

| # | Candidate (LinkedIn slug) | ZoomInfo | Exa | Apollo | Phone | Email |
|---|---|---|---|---|---|---|
| 1 | muhilan-j-714b9b344 | skipped | **timeout >60s** | 422 | ❌ | — |
| 2 | veeraswamy0556 | skipped | ✅ phone+email | (not called) | ✅ `***4861` | unisoftllc.com |
| 3 | abhilashofficial4 | skipped | email-only | 422 | ❌ | jpmchase.com |
| 4 | chakradhar-chitta-272799306 | skipped | **timeout >60s** | 422 | ❌ | — |
| 5 | purna-chandu-chirakala-517091185 | skipped | nothing | 422 | ❌ | — |
| 6 | sivaram-krishna-garikapati-992985319 | skipped | email-only | 422 | ❌ | citi.com |
| 7 | prajapatimudra | skipped | email-only | 422 | ❌ | jpmchase.com |
| 8 | venkata-…-puvvada-a351882b3 | skipped | ✅ phone+email | (not called) | ✅ `***4650` | jpmorgan.com |
| 9 | shaikshapuruddin | skipped | **timeout >60s** | 422 | ❌ | — |

| Metric | Count | Rate |
|---|---|---|
| **Phone number** | 2 / 9 | **~22%** |
| Work email | 6 / 9 | ~67% |
| Nothing at all | 3 / 9 | ~33% |
| Apollo `422 insufficient credits` | every call | 100% |
| ZoomInfo fired | 0 | never |
| Exa `timed out >60s` | 3 / 9 | ~33% |

Both phones observed came **only** from Exa. Apollo and ZoomInfo each contributed **zero**.

---

## The enrichment chain (as it runs on this branch)

On-demand / Step-5 launch path: `_enrich_candidate_contact_impl` in
[`apps/api/routers/candidates.py`](../apps/api/routers/candidates.py) — order is **ZoomInfo-by-email → Exa-by-URL → Apollo-by-URL**, each step gated by a cost short-circuit (`_have_email_and_phone()`), attribution = first contributor:

1. **ZoomInfo by email** — `contact_enrichment.zoominfo_enrich_by_email`, only when a seed email exists. URL-only Exa candidates have none → skipped.
2. **Exa Agent by LinkedIn URL** — `contact_enrichment.exa_enrich_by_linkedin` (raw `POST https://api.exa.ai/agent/runs`, poll until `completed`). `EXA_CONTACT_ENRICH_ENABLED=True`, `EXA_CONTACT_ENRICH_EFFORT=low`, `EXA_CONTACT_ENRICH_TIMEOUT_S=60`.
3. **Apollo by LinkedIn URL** — `contact_enrichment.apollo_enrich_by_linkedin`, fills remaining gaps.

Inline **sourcing-time** path (during the search itself):
`contact_enrichment.enrich_contact_for_sourcing` in
[`apps/api/services/contact_enrichment.py`](../apps/api/services/contact_enrichment.py),
called from `unified_candidate_search.py`. Order is **ZoomInfo-by-name → Apollo-by-URL** — **no Exa here**. Capped at `PER_JOB_CAP = 50` per job. During the observed run this path hit the 50-cap and every attempt returned empty (ZoomInfo silent-miss, Apollo 422).

---

## Provider-by-provider analysis

### 1. Apollo — DEAD (out of credits)

`APOLLO_API_KEY` is **not set in env**, so the code falls back to the legacy in-repo key
(`contact_enrichment.py`, the `APOLLO_KEY_SOURCE = "legacy_fallback"` WARN fires at import).
That key is out of credits — every call returns `422`:

```
WARNING services.contact_enrichment: Apollo non-2xx for exa_linkedin.com/in/abhilashofficial4:
422 {"error":"You have insufficient credits! Upgrade your plan to increase your number of lead credits."}
```

Observed in **both** the inline-sourcing path and the launch path. This removes the reliable
phone backfill — every email-only Exa result (rows 3, 6, 7) that Apollo *should* have completed
with a phone instead stays phone-less.

### 2. ZoomInfo — never invoked for these candidates

The launch path only matches ZoomInfo **by email** (`zoominfo_enrich_by_email`). Exa-sourced
candidates have no seed email, so the step is skipped — `ZoomInfo enrich-by-email …` never
appears in the logs once. ZoomInfo OAuth **is** configured (`ZOOMINFO_CLIENT_ID` etc. present),
so this is a wiring gap, not a credentials problem: the launch path has **no ZoomInfo-by-name
step**, even though `full_name` is available (the sourcing path already does ZoomInfo-by-name).

### 3. Exa — only live provider, weak on phone

Exa returns a **work email** reliably (jpmchase / citi / jpmorgan / unisoftllc), but:

- **Phone comes back only ~⅓ of completions.** The phone is the expensive billable field
  (~$0.07/run); at `effort=low` the agent frequently returns email-only:
  ```
  INFO routers.candidates: Contact enrich parsed for exa_linkedin.com/in/abhilashofficial4 |
  provider=exa | final_outcome=enriched | phone_source=none | has_mobile=False | has_email=True |
  email=a***@jpmchase.com
  ```
- **It times out at the 60s cap on ~⅓ of candidates** → returns nothing:
  ```
  INFO services.contact_enrichment: Exa agent run timed out for exa_linkedin.com/in/muhilan-j-714b9b344 (>60s)
  ```
  When Exa is the *only* live provider and it times out, the candidate gets **no contact at all**.
- **When it does return a phone, it works:**
  ```
  INFO routers.candidates: Contact enrich parsed for exa_linkedin.com/in/veeraswamy0556 |
  provider=exa | final_outcome=enriched | phone_source=mobilePhone | has_mobile=True |
  has_email=True | mobile=***4861 | email=v***@unisoftllc.com
  ```

---

## Recommended fixes (impact order)

1. **Fund / set a valid `APOLLO_API_KEY`** in `apps/api/.env` (and prod env).
   *Highest impact.* Apollo is the reliable phone-by-LinkedIn-URL provider and currently
   contributes zero. Restoring it directly recovers the email-only cases (rows 3, 6, 7) and
   most empties. Setting the env var also silences the `legacy_fallback` WARN.

2. **Add a ZoomInfo-by-*name* step to the launch chain.**
   `full_name` is available; the sourcing path already does `_zoominfo_resolve_person_id` +
   `_zoominfo_enrich_by_person_id`. The launch path keys ZoomInfo on email only, so a configured,
   paid provider sits idle for every URL-only candidate. Adding a by-name fallback gives a second
   real phone source.

3. **Do NOT raise Exa effort to compensate.**
   Exa already times out at 60s on `low` effort; raising effort would make timeouts worse. If
   leaning on Exa, make the run **non-blocking** (kick off the run, persist the run id, poll
   later / via callback) so a slow run doesn't block the launch and doesn't yield zero.

---

## Secondary observations (not the phone root cause)

- **This branch lacks a latency fix that exists elsewhere.** Order here is
  `ZoomInfo → Exa → Apollo` with a **60s** Exa timeout. A separate change (Apollo moved ahead of
  Exa + timeout lowered to 25s + parallelized Step-5 enrich loop) is **not on this branch/main**.
  Consequence: launches block up to 60s per candidate on Exa even when it returns nothing.

- **Exa Agent *search* (pass-B) is broken in the local venvs.**
  ```
  WARNING services.exa_service: Exa Agent create failed: 'Exa' object has no attribute 'beta'
  ```
  Both local venvs (`.venv`, `apps/api/venv`) have **`exa_py 2.12.0`**, but
  `services/exa_service.py` calls `self.exa.beta.agent.runs.*` which needs **`exa_py >= 2.13.0`**
  (pinned in `requirements.txt`). This only reduces *extra candidate discovery* via the agent —
  the primary `exa.search()` still returned 50 candidates, and **contact enrichment is unaffected**
  (it uses raw httpx, not the SDK's `.beta`). Fix locally: `pip install 'exa_py>=2.13.0'`.

---

## Appendix — how to reproduce

1. Start servers (single worker keeps logs clean and avoids the multi-worker scheduler stampede):
   - `api`: `uvicorn main:app --host 127.0.0.1 --port 8000 --app-dir apps/api` (with `apps/api/.env`)
   - `web`: `npm run dev --prefix apps/web` (needs `apps/web/.env.local` → `NEXT_PUBLIC_API_URL=http://localhost:8000`)
2. Launch an Exa candidate search and proceed to the Step-5 launch contact-enrichment.
3. Watch the API logs, filtering for: `Contact enrich providers`, `Contact enrich parsed`,
   `Apollo non-2xx`, `Exa agent run timed out`, `ZoomInfo enrich-by-email`.
