# Exa cost: where it goes and what we're cutting (2026-10-14)

This research made no billable calls. Sources:
- The code on main (PROD) and develop (QA).
- The free `GET https://api.exa.ai/agent/runs` list: all 54,020 runs from 2026-06-17 to 2026-10-13, each with its `costDollars`.
- New Relic `PipelineStage` events on PROD.
- Exa's pricing docs (exa.ai/docs/admin/pricing.md, exa.ai/pricing), fetched on 2026-10-14.

## Prices that matter
| Item | Price | Note |
|---|---|---|
| Agent run, fixed effort | minimal $0.012 · low $0.025 · medium $0.10 · high $0.50 · xhigh $1.00 | A **flat fee per run**, even if the run finishes early. Our data matches exactly: every low run billed $0.025 and every medium run $0.10. Cancelling a run saves nothing. |
| Contact enrichment | $0.02 per email found · $0.07 per phone found | Charged on top of the run fee. |
| Fiber.ai (Exa Connect) | $0.02 a credit: person lookup 2, work email 2, phone 5 | A lookup that finds nothing is free. About $0.064 per deep run measured. |
| Search `auto` / `fast` | $7 per 1k requests, covering 10 results; +$1 per 1k for each extra result | Our 50-result people query costs $0.047 for search alone. |
| Contents (text / highlights / summary) | $1 per 1k pages, per view | Bundled for the first 10 results since 2026-03-03. **Billing for results 11–50 is undocumented**, so a Pass A call costs $0.047–0.127. |
| Errors | 402 and 503 are not billed | |
| Limits | 50 active agent runs per account; search 10 QPS (25 QPS after $1k of credit purchases in 30 days) | We hit 51 active runs on 2026-10-05. |

## Measured spend: 15 days, 2026-09-29 to 10-13 (11 weekdays)
Agent runs: 31,123 runs costing **$2,781.56**, about $251 per weekday and **about $5.5k a month**.

| Surface | 15-day $ | Share | Hit rate |
|---|---|---|---|
| Normal contact run (low) | $1,407 | 50.6% | email 48%, phone 79% |
| Deep contact run (medium + Fiber) | $1,249 | 44.9% | email 53%, phone 16%; **46% of deep spend found nothing** |
| Pass B sourcing sweep (medium) | $124 | 4.5% | about 100% thrown away (see below) |

**Pass A people search** (`search_and_contents`) does not appear in the run list, and its cost is never logged. PROD PipelineStage events show:
- About 2.8 calls per search run and about 660 billed calls per business day.
- That works out to about **$0.72–2.0k a month**.
- About half the runs are sample previews, which keep 12 of up to 150 fetched profiles.

Monthly total: about $6.3–7.6k.

### The wastes
1. **Pass B is paid for, then discarded.**
   - exa_py 2.13 declares `AgentUsage.agent_compute_units: Optional[int]`, but Exa now returns `0.4`. Create and poll both raise.
   - `deep_research_candidates()` then returns `[]` and the run id is lost.
   - On PROD, 187 of 234 creates failed this way, and every Pass B run in the window billed 0.4 ACU.
   - Separately, 47% of Pass B prompts were exact repeats: relaxation attempts send identical prompts.
2. **Deep email runs for people we can already reach.** The most common per-person sequence is: normal email miss, then normal phone found, then a deep email run (3,386 times per 15 days). That costs **about $1.6k a month at about $0.30 per email found**, and the Launch gate is phone **or** email.
3. **Paying twice for one person.**
   - 8,437 sourcing email runs were later followed by a separate Launch phone run, a second $0.025 base fee each.
   - 266 exact duplicate queries (27 created while an identical run was still in flight).
   - 238 re-asks of a field already found, and 184 re-asks of a cached miss.
   - Recruiter clicks re-buy deep misses.
4. **Pass A over-fetch.**
   - Up to 3 title queries each buy 50 results, and the merge keeps 50.
   - Sample, full, relaxation and launch-session resume runs re-buy identical queries, because there is no cache.
5. **Silent outages.**
   - Every Exa path fails open to `[]`.
   - The key returned `402 NO_MORE_CREDITS` from 2026-10-13 19:56 UTC (130 PROD searches across 28 jobs ran without Exa), and earlier for 49.6 h on 9/21–23.
   - No alert fired either time: the New Relic policy has no notification destination.

## Decisions (2026-10-14)
- Turn Pass B off. Keep the per-URL `get_contents` deep analysis off too (`EXA_AGENT_ENABLED` and `EXA_DEEP_ANALYSIS_ENABLED`, both default false). Turning Pass B off alone switches deep analysis on.
- Skip the Launch deep email run when a personal phone is already held. Ship it as a 50/50 holdout keyed on a hash of the LinkedIn slug, then decide.
- Defer sourcing-time paid Exa email lookups on Source & Launch full runs and launch-session S1. Browse-only Step-5 searches keep buying.
- Found contacts never expire. A deep-search miss is final for 180 days on automated paths. A recruiter click on a deep-missed person must confirm before buying again.
- Instrument every Exa call (`ExaCall` New Relic event with `costDollars`). Add an out-of-credits breaker and alerts.
- Right-size Pass A `num_results` and cache Pass A results in Redis.
- Out of scope for now: an effort-tier A/B (low vs medium deep), Enterprise pricing talks, Kipplo/ZoomInfo ahead of Exa.

## Re-measuring
`GET /agent/runs` is free. Page it with raw httpx, not the SDK list (it fails the same int parse on 2.13):
- `x-api-key` header, `limit=100`, cursor pagination.
- Each run carries `costDollars` (agentCompute, enrichments, dataSources) and `usage`.

Contact runs now carry `metadata` (`env/surface/tier/path/trigger/job_id/attempt/arm/call_id`). `apps/api/scripts/exa_runs_ledger.py` does the weekly reconcile: it groups spend by `metadata.env/surface/tier/trigger/arm` and, given the `exa_run_id`/`call_id`s New Relic saw finish (completed, failed or cancelled), flags billed runs no `ExaCall` event saw end. Never store or print `request.query` or `output`: they contain candidate names.

Live spend: New Relic `ExaCall` events (cost per call, by surface / tier / trigger) on the PAIR Ops dashboard's "Exa" page; runbook and kill switches in `docs/observability/new-relic.md`.
