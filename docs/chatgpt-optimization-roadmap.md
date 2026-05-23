# ChatGPT / OpenAI cost-optimization analysis

## Context

The airecruiter API makes OpenAI calls from ~12 different call sites with **no centralized client wrapper, no Redis, and almost no response caching**. The only dedup that exists today is a Postgres `resume_hash` cache used at *save* time in `candidate_enhanced_info` — but the actual LLM call sites (Tribunal, ranking, refresh-match) don't consult it before firing the model. Every Tira chat message replays the same ~240-word system prompt, every Tribunal verdict re-pays for the same ~550-word adversarial-debate prompt, and every JD generation replays the same ~1700-word copywriter prompt. None of these prompts use OpenAI's prompt-caching feature, even though most are stable across calls.

This document is an **analysis + ranked roadmap only** (no code changes). It identifies where ChatGPT costs are being burned today, what's safely cacheable, and what to ship first. The proposed cache backend is **Redis** (new dependency), to support TTL semantics and prompt-caching tier tracking that the existing Postgres pattern doesn't fit cleanly.

---

## Inventory: where ChatGPT is called

| # | Call site | Model | Temp | Approx tokens in | Frequency | Stable prefix? | Cached today? |
|---|-----------|-------|------|------------------|-----------|----------------|---------------|
| 1 | `services/tribunal.py:105` – Skeptic-vs-Advocate verdict | gpt-4o-mini | 0.2 | 5k–8k | per (candidate × job) match | system prompt (~550 words) yes | **No** |
| 2 | `services/ai_service.py:93` – resume → CandidateProfile | gpt-4o-mini | 0.0 | 8k–12k | per candidate ingest | small system prompt | resume_hash exists but **not consulted at this call site** |
| 3 | `services/ai_service.py:49` – JD → structured | gpt-4o-mini | 0.0 | 1k–4k | per JD parse | yes | No |
| 4 | `services/ai_service.py:289` – profile → resume text | gpt-4o-mini | 0.3 | 1k | on-demand | yes | No |
| 5 | `services/enhanced_job_extractor.py:103` – **full JD extraction** | **gpt-4o** | 0.0 | 1.5k–2.5k | per JD | ~1100-word system | No |
| 6 | `services/job_skills_extractor.py:342` – rubric phase 2 | gpt-4o-mini | 0.2 | 3k–5k | per rubric generation | **~2300-word system prompt** | No |
| 7 | `services/screening_question_generator.py:702` | gpt-4o-mini | 0.5 | 2k–3.5k | per question generation | family-specific (deterministic given family) | No |
| 8 | `services/chat_service.py:264` + `:308` – Tira chat (2 LLM calls per user turn) | gpt-4o-mini | default | 0.5k–2k | per user message | **~240-word system prompt identical every call** | No |
| 9 | `services/location.py:270` – proximity check | gpt-4o-mini | 0.0 | ~150 | per candidate scoring (skipped if remote) | yes | in-process dict only, not persisted |
| 10 | `routers/ai_generation.py:312` – JD generation copy | gpt-4o (env-overridable) | 0.3 | 2.5k–4k | per JD enhance | ~1700-word system | No |
| 11 | `routers/ai_generation.py:394` – job title polish | gpt-4o (env-overridable) | 0.3 | ~400 | per title polish | yes | No |
| 12 | `routers/tira.py:226` – boolean search from JD | gpt-4o-mini | n/a | ~500 | per click | yes | No |
| 13 | `services/skill_embeddings.py:87` – `text-embedding-3-small` batch | embedding | n/a | 256 terms/batch | per warm | In-process LRU (50k entries), lost on restart | Partial |
| 14 | `services/azure_agent_service.py:137` – Azure skill/role extract | Azure agent | n/a | 6k–10k | per JD skills extract | n/a (rate-limited to 1 concurrent) | No |

**Nine separate `AsyncOpenAI` clients** are instantiated across the services (no shared singleton), each with its own connection pool.

---

## Ranked optimization roadmap

Estimates assume current order-of-magnitude volume (rough; precise sizing belongs in a separate cost audit). Effort: S = <1 day, M = 1–3 days, L = 3+ days.

### Tier 1 — Big wins, low risk

1. **Cache Tribunal verdicts by `(resume_hash, job_rubric_hash)`** — Redis, 7-day TTL
   - File: `apps/api/services/tribunal.py` (wrap `evaluate_narrative`)
   - Inputs: `resume_text`, candidate TOON, JD TOON, optional distance/radius. Hash a stable subset (resume_hash already exists; rubric hash = sha256 of canonical-form skills+titles+education).
   - Why it's safe: temperature 0.2, deterministic schema-parse, `_fail_open` already returns synthetic verdict — caching the success path is strictly an improvement.
   - **Savings**: largest single line item — Tribunal fires once per (candidate, job) on ranking and refresh-match. Eliminates re-charging on bulk re-score and on returning users.
   - Effort: **S**

2. **OpenAI prompt caching for Tira chat**
   - File: `apps/api/services/chat_service.py:22-34` (system prompt) and `:264`, `:308` (both rounds).
   - The system prompt + tool schemas are byte-identical every call. Restructure messages so the system prompt + tools block stays first (already does) and add the `prompt_cache_key` parameter so OpenAI's automatic prefix cache hits. No SDK changes beyond adding the cache key.
   - **Savings**: ~50% on the prefix tokens (OpenAI's cached input is half-price) for both rounds.
   - Effort: **S**

3. **OpenAI prompt caching for the three "fat-prompt" extractors**
   - `services/job_skills_extractor.py:342` (2300-word system) — single biggest cacheable prefix
   - `services/enhanced_job_extractor.py:103` (1100-word system)
   - `routers/ai_generation.py:312` (1700-word system)
   - All three have stable system prompts that vary only by the user-content block. Same approach as Tira: pass `prompt_cache_key`.
   - **Savings**: ~50% on the system-prompt tokens (which dominate input cost for these calls).
   - Effort: **S**

4. **Honor `resume_hash` at the parse call site, not just at save**
   - File: `apps/api/services/ai_service.py:93` (`_extract_candidate`)
   - Today: `_lookup_cached_enhanced_info_by_resume_hash` exists in `services/sourced_candidates_storage.py:812` and is only checked inside `save_candidate_enhanced_info`. The actual LLM call always fires.
   - Fix: check the cache *before* `client.beta.chat.completions.parse(...)`. If hit, materialize the CandidateProfile from the cached row and skip the LLM. The cache already has a 30-day TTL column.
   - **Savings**: roughly eliminates duplicate parsing for re-ingested resumes (very common — JobDiva applicant sync runs every 15min, refresh-match re-parses, etc.).
   - Effort: **S**

### Tier 2 — Moderate wins, slightly more care

5. **Centralize the OpenAI client into a singleton wrapper**
   - New file: `apps/api/core/llm_client.py`. Single `AsyncOpenAI` with shared `httpx.AsyncClient`, exposes `chat_complete(...)`, `parse(...)`, `embed(...)` with built-in: retry/backoff, Redis response cache (opt-in per call), prompt-cache-key plumbing, per-call timing/cost logging.
   - Migrate the 9 existing client instantiations to consume it.
   - Why: every later optimization is easier when there's one place to add it. Today, instrumenting cost or adding caching means touching 9 files.
   - **Savings**: enables tiers 1, 3, and observability. Direct token savings: 0.
   - Effort: **M**

6. **Cache rubric + screening questions per job**
   - Files: `services/job_skills_extractor.py:342`, `services/screening_question_generator.py:702`
   - Key: `sha256(canonicalized JD text + family + level)`. Redis 30-day TTL.
   - Invalidate when JD text changes (already known at the `ai_generation.py` save site).
   - **Savings**: recruiters often regenerate rubric/questions on the same JD; right now each click pays full cost.
   - Effort: **S**

7. **Cache boolean search by JD hash**
   - File: `routers/tira.py:226`
   - Key: `sha256(jd_text[:5000])`. Redis 1-day TTL.
   - **Savings**: small per-call (~500 tokens) but free.
   - Effort: **S**

8. **Cache location-proximity verdicts persistently**
   - File: `services/location.py:270`
   - Today in-process dict only — lost on every worker restart. Same call is repeated millions of times for `(candidate_city, job_city, radius)` tuples.
   - Move to Redis (long TTL — geography doesn't change).
   - **Savings**: high call volume × cheap call = decent steady savings. Also kills the in-process unbounded dict.
   - Effort: **S**

### Tier 3 — Larger structural changes

9. **Move embedding cache from in-process LRU to Redis**
   - File: `services/skill_embeddings.py:36-38` (current OrderedDict, 50k entries, per-worker, lost on restart)
   - Replace with Redis SET-based vector cache. Survives restarts; shared across workers.
   - **Savings**: avoids re-embedding the same skill terms after every deploy / worker restart.
   - Effort: **M**

10. **Drop the two `gpt-4o` call sites to `gpt-4o-mini`** (see model-tiering section below)
    - File: `services/enhanced_job_extractor.py:103`, `routers/ai_generation.py:312` and `:394`
    - These are the only two services still on full gpt-4o. JD generation may be worth keeping on gpt-4o for copywriting fluency — A/B first.
    - **Savings**: ~94% on the enhanced extractor call, ~94% on the title polish call, similar on JD generation if mini holds quality.
    - Effort: **S** (config flip + sample-set A/B)

11. **Push the simplest structured-extraction calls to `gpt-4.1-nano`** (see model-tiering section below)
    - Files: `services/ai_service.py:49` (JD → structured), `services/location.py:270` (proximity), `routers/tira.py:226` (boolean), `routers/ai_generation.py:394` (title polish).
    - These are mechanical schema-fill / yes-no / short-transform calls. Nano is roughly 1/3 the cost of mini.
    - **Risk**: nano can drop accuracy on nested schemas — validate each before flipping.
    - Effort: **S** per call site

12. **Use OpenAI Batch API for nightly applicant ingestion**
    - File: `routers/candidate_processing.py` (`extract_all_job_applicants`)
    - JobDiva applicant sync is async/non-blocking by nature — 24h Batch API turnaround is acceptable.
    - **Savings**: 50% on this code path.
    - Effort: **L** (requires job queue + result-merge logic)

---

## Model tiering (target end state)

User direction: prefer **`gpt-4o-mini` or `gpt-4.1-nano`** wherever the task allows. Haiku 4.5 was considered and **rejected** — at current pricing (~$1/M input, $5/M output) it's materially more expensive than both `gpt-4o-mini` (~$0.15/$0.60) and `gpt-4.1-nano` (~$0.10/$0.40), so it offers no cost win for tasks this lightweight.

The rule of thumb applied below: **nano** for mechanical schema-fill / classification / short text transforms; **mini** for tasks that need any chain of reasoning, persona, or fluent writing; **gpt-4o** only kept as an A/B option for the copywriting JD generation, where fluency may justify it.

| Call site | Today | Target | Rationale |
|-----------|-------|--------|-----------|
| `tribunal.py:105` Skeptic-vs-Advocate | mini | **mini** (keep) | Multi-persona reasoning + nuanced tag selection — too risky for nano |
| `ai_service.py:93` resume → CandidateProfile | mini | **mini** (keep) | Long, messy resumes; nested schema; worth the accuracy margin |
| `ai_service.py:49` JD → structured | mini | **nano** | Straight schema fill from a JD string |
| `ai_service.py:289` profile → resume text | mini | **mini** (keep) | Short, but it's generative text — keep mini |
| `enhanced_job_extractor.py:103` full JD extract | **gpt-4o** | **mini** | No reasoning advantage justifies 25× the cost here |
| `job_skills_extractor.py:342` rubric phase 2 | mini | **mini** (keep) | Complex rules (IT vs non-IT, family aliases, education) — nano will drop signal |
| `screening_question_generator.py:702` | mini | **mini** (keep) | Creative + constrained; family-aware shot examples |
| `chat_service.py:264` + `:308` Tira chat | mini | **mini** (keep) | Tool-calling + free-form replies |
| `location.py:270` proximity check | mini | **nano** | Two strings in, yes/no + short reason out |
| `ai_generation.py:312` JD generation copy | **gpt-4o** | **mini** (with gpt-4o A/B fallback) | Copywriting — start mini, fall back to gpt-4o only if recruiter satisfaction drops |
| `ai_generation.py:394` title polish | **gpt-4o** | **nano** | Trivial text transform under 60 chars |
| `tira.py:226` boolean from JD | mini | **nano** | JSON with must-haves / nice-to-haves / exclusions; mechanical |
| `taxonomy_service.py` discovery | mini | **mini** (keep) | Free-form skill identification with quality bar |
| Embeddings | text-embedding-3-small | **keep** | Already the cheap tier |

**Wiring**: the new `core/llm_client.py` wrapper (Tier 2 #5) should expose model selection per call, with the chosen model encoded next to the prompt — not via a single `OPENAI_MODEL` env var. The env var stays as a global override for emergencies.

**Validation gate for each nano flip**: run 20 sample inputs through both mini and nano, diff the parsed outputs, only flip if zero meaningful regressions. The Tier 2 #5 wrapper's per-call logging makes this a one-day exercise per call site.

### Won't-do (yet)

- **Streaming** — no streaming today; would help UX but doesn't reduce token cost. Defer.
- **Caching JD-generation output** — recruiter notes vary per request, hit rate would be low. Prompt caching (Tier 1 item 3) covers this.

---

## Proposed cache architecture (Redis)

- New env vars: `REDIS_URL` (required for cache features), `LLM_CACHE_ENABLED` (kill switch).
- Key namespacing:
  - `llm:tribunal:v1:{resume_hash}:{rubric_hash}` → JSON verdict (7d TTL)
  - `llm:rubric:v1:{jd_hash}` → JSON rubric (30d TTL)
  - `llm:screening:v1:{jd_hash}:{family}:{level}` → JSON questions (30d TTL)
  - `llm:boolean:v1:{jd_hash}` → string (1d TTL)
  - `llm:location:v1:{cand_loc}:{job_loc}:{radius}` → JSON (never expire; geography stable)
  - `llm:embed:v1:{term}` → bytes (vector) (never expire)
- `prompt_cache_key` (OpenAI's own prefix cache, not Redis): one stable key per logical prompt template (`"tira-chat-v1"`, `"tribunal-v1"`, etc.) so OpenAI's internal prefix cache hits across users.
- All cache reads/writes go through the new `core/llm_client.py` wrapper from Tier 2 #5.

---

## Critical files to modify (when implementation begins)

These are the files the roadmap items above point at; listed once for reference:

- `apps/api/core/llm_client.py` (new — singleton wrapper)
- `apps/api/core/config.py` (add `REDIS_URL`, `LLM_CACHE_ENABLED`)
- `apps/api/services/tribunal.py` (Tier 1 #1)
- `apps/api/services/chat_service.py` (Tier 1 #2)
- `apps/api/services/job_skills_extractor.py` (Tier 1 #3, Tier 2 #6)
- `apps/api/services/enhanced_job_extractor.py` (Tier 1 #3, Tier 3 #11)
- `apps/api/routers/ai_generation.py` (Tier 1 #3)
- `apps/api/services/ai_service.py` (Tier 1 #4, Tier 3 #10)
- `apps/api/services/sourced_candidates_storage.py` (already has `_lookup_cached_enhanced_info_by_resume_hash` at :812 — reuse, don't duplicate)
- `apps/api/services/screening_question_generator.py` (Tier 2 #6)
- `apps/api/routers/tira.py` (Tier 2 #7)
- `apps/api/services/location.py` (Tier 2 #8)
- `apps/api/services/skill_embeddings.py` (Tier 3 #9)
- `apps/api/services/taxonomy_service.py` (Tier 3 #10)

---

## Existing utilities to reuse (don't re-invent)

- `_lookup_cached_enhanced_info_by_resume_hash(resume_hash)` — `sourced_candidates_storage.py:812`
- `_resume_text_hash(text)` — same file (already used at `:1143`)
- `core.toon.encode` — TOON encoder, already keeps Tribunal inputs compact
- `core.config.LLM_CONCURRENCY` semaphore — used for concurrent extraction limits
- APScheduler in `apps/api/main.py` — available for periodic cache warming / Batch API result polling without adding a new queue system

---

## Verification (for the eventual implementation phase)

1. **Unit-level**: For each cached call site, add a test that calls it twice with identical inputs and asserts the OpenAI client mock was called once. (No new test framework needed — pytest already in use.)
2. **Cost telemetry**: The new `core/llm_client.py` wrapper logs `{model, prompt_tokens, completion_tokens, cache_hit, prompt_cache_hit, duration_ms}` per call. Diff a baseline day vs. a post-deploy day in logs.
3. **Quality sentinels**: For Tier 1 #1 (Tribunal cache) and Tier 3 #10/#11 (model swaps), pick 20 known (candidate, job) pairs, store baseline verdicts/extractions, re-run after change, eyeball delta. Hand-pick — no automated golden tests needed for an analysis-stage roadmap.
4. **Kill switches**: `LLM_CACHE_ENABLED=false` must fully bypass Redis so a bad cache entry can't pin a regression.
5. **Manual smoke test**: Tira chat → JD generation → rubric → screening Qs → score candidate → engage. The full recruiter flow, with cache log lines watched.

---

## Out of scope for this plan

- Implementation. This document is the roadmap; per the user's selection, no code changes follow.
- Precise dollar/month savings — would require a 7-day cost export from the OpenAI dashboard. The roadmap is ranked by relative impact (token volume × call frequency × cacheability), not absolute $.
- Frontend (`apps/web`) changes — no LLM calls there.
- Anthropic SDK adoption beyond the optional Tier 3 #10 model swap.
