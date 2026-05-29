import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(dotenv_path=env_path, override=True)

def get_env_or_fail(var_name: str) -> str:
    """Retrieve an environment variable or raise an error if not set."""
    val = os.getenv(var_name)
    if val is None:
        raise RuntimeError(f"Required environment variable '{var_name}' is not set.")
    return val

def get_env_with_default(var_name: str, default: str) -> str:
    """Retrieve an environment variable with a default value."""
    return os.getenv(var_name, default)

def get_env_bool(var_name: str, default: bool = False) -> bool:
    """Retrieve a boolean env var with common truthy values."""
    val = os.getenv(var_name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}

# ---- API/Environment Settings ----
ALLOWED_ORIGINS = get_env_with_default("ALLOWED_ORIGINS", "*").split(",")

# AI Models (Configurable for deployment flexibility)
# Default to gpt-4o-mini: ~16× cheaper than gpt-4o on input/output tokens with
# comparable quality for structured resume extraction. Override via env if a
# larger model is needed for a specific deployment.
OPENAI_MODEL = get_env_with_default("OPENAI_MODEL", "gpt-4o-mini")

# ---- LLM Runtime Tuning ----
# Maximum concurrent outbound LLM calls (crisp + extract). The previous default
# of 2 was set to dodge 429s under gpt-4-turbo; gpt-4o-mini tier comfortably
# handles 5-8 concurrent. Raise via env for higher-tier accounts.
LLM_CONCURRENCY = int(get_env_with_default("LLM_CONCURRENCY", "5"))

# When true, skip LLM extraction for candidates that already have structured
# skills + company history + title from the source API (e.g. LinkedIn/Unipile).
# Set to "false" to always run LLM extraction.
SKIP_LLM_IF_STRUCTURED = get_env_with_default("SKIP_LLM_IF_STRUCTURED", "false").lower() == "true"

# ---- LLM response cache (Redis) ----
# Backs the per-call response caches added in tier 1 (Tribunal verdicts,
# parsed candidate profiles) and tier 2 (rubric / screening / boolean /
# location). When REDIS_URL is empty the cache is a no-op — every call
# falls through to the LLM as before. LLM_CACHE_ENABLED is a kill switch
# that bypasses Redis without unsetting REDIS_URL (useful for evicting a
# bad entry without redeploying).
REDIS_URL = get_env_with_default("REDIS_URL", "")
LLM_CACHE_ENABLED = get_env_bool("LLM_CACHE_ENABLED", True)

# Debugging
DEBUG_LOG_PATH = os.getenv("DEBUG_LOG_PATH")
OPENAI_API_KEY = get_env_or_fail("OPENAI_API_KEY")

# JobDiva Configuration
JOBDIVA_API_URL = get_env_with_default("JOBDIVA_API_URL", "https://api.jobdiva.com")
JOBDIVA_CLIENT_ID = get_env_or_fail("JOBDIVA_CLIENT_ID")
JOBDIVA_USERNAME = get_env_or_fail("JOBDIVA_USERNAME")
JOBDIVA_PASSWORD = get_env_or_fail("JOBDIVA_PASSWORD")

# Unipile Configuration
UNIPILE_API_KEY = get_env_or_fail("UNIPILE_API_KEY")
UNIPILE_DSN = get_env_with_default("UNIPILE_DSN", "api1.unipile.com")
UNIPILE_ACCOUNT_ID = get_env_or_fail("UNIPILE_ACCOUNT_ID")

# ---- Database Settings ----
DATABASE_URL = get_env_or_fail("DATABASE_URL")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
if SUPABASE_DB_URL and SUPABASE_DB_URL.startswith("postgres://"):
    SUPABASE_DB_URL = SUPABASE_DB_URL.replace("postgres://", "postgresql://", 1)

DB_USER = get_env_with_default("DB_USER", "postgres")
DB_PASSWORD = get_env_with_default("DB_PASSWORD", "password")
DB_NAME = get_env_with_default("DB_NAME", "skills_db")
CLOUDSQL_CONNECTION_NAME = os.getenv("CLOUDSQL_CONNECTION_NAME")
INSTANCE_CONNECTION_NAME = CLOUDSQL_CONNECTION_NAME

# ---- UDF Mappings ----
JOBDIVA_AI_JD_UDF_ID = int(get_env_with_default("JOBDIVA_AI_JD_UDF_ID", "230"))
JOBDIVA_JOB_NOTES_UDF_ID = int(get_env_with_default("JOBDIVA_JOB_NOTES_UDF_ID", "231"))

# ---- PAIR Recruiter & Qualifications ----
# Set these in .env to match your JobDiva account configuration.
JOBDIVA_PAIR_RECRUITER_ID = int(get_env_with_default("JOBDIVA_PAIR_RECRUITER_ID", "0"))
JOBDIVA_PAIR_QUALIFICATION_NAME = get_env_with_default("JOBDIVA_PAIR_QUALIFICATION_NAME", "PAIR Candidates")
JOBDIVA_PAIR_QUALIFICATION_ID = int(get_env_with_default("JOBDIVA_PAIR_QUALIFICATION_ID", "0"))
JOBDIVA_PASS_ACTION_NAME = get_env_with_default("JOBDIVA_PASS_ACTION_NAME", "PAIR Pass Candidate Report")
JOBDIVA_PASS_QUALIFICATION_VALUE = get_env_with_default("JOBDIVA_PASS_QUALIFICATION_VALUE", "Pass")

# ---- Encryption ----
ENCRYPTION_KEY = get_env_or_fail("ENCRYPTION_KEY")
ENCRYPTION_SALT = os.getenv("ENCRYPTION_SALT")

# ---- Azure AI Agent (skill-role-extractor) ----
AZURE_AI_PROJECT_ENDPOINT = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "")
AZURE_OPENAI_API_KEY      = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_AI_AGENT_NAME       = os.getenv("AZURE_AI_AGENT_NAME", "skill-role-extractor")

# ---- Exa API ----
EXA_API_KEY = get_env_with_default("EXA_API_KEY", "")

# ---- Apify (LinkedIn Open-to-Work enrichment for Exa-sourced candidates) ----
APIFY_API_TOKEN = get_env_with_default("APIFY_API_TOKEN", "")
APIFY_LINKEDIN_OTW_ACTOR = get_env_with_default(
    "APIFY_LINKEDIN_OTW_ACTOR", "freshdata/linkedin-open-to-work-status"
)

# ---- Oxylabs Web Scraper API ----
# HTTP Basic credentials for https://realtime.oxylabs.io/v1/queries.
OXYLABS_USERNAME = get_env_with_default("OXYLABS_USERNAME", "")
OXYLABS_PASSWORD = get_env_with_default("OXYLABS_PASSWORD", "")

# ---- Monster recruiter portal (Power Resume Search) ----
# Used to authenticate Oxylabs-rendered sessions into Monster's resume DB.
# When unset, the Monster provider short-circuits with an empty result list.
MONSTER_USERNAME = get_env_with_default("MONSTER_USERNAME", "")
MONSTER_PASSWORD = get_env_with_default("MONSTER_PASSWORD", "")
# Override only if Monster moves the recruiter portal. The defaults match the
# current hiring.monster.com surface as of 2026-05.
MONSTER_LOGIN_URL = get_env_with_default(
    "MONSTER_LOGIN_URL", "https://hiring.monster.com/recruiter/login"
)
MONSTER_SEARCH_URL = get_env_with_default(
    "MONSTER_SEARCH_URL", "https://hiring.monster.com/talent-search/results"
)
# Per-search pagination cap. Each page is one Oxylabs billable request, so this
# directly bounds cost per /candidates/search call against the Monster source.
MONSTER_MAX_PAGES = int(get_env_with_default("MONSTER_MAX_PAGES", "2"))

# ---- ZoomInfo Contact Enrichment ----
# Auth: OAuth2 client_credentials flow against ZoomInfo's Okta auth server.
# `services.zoominfo_auth.get_access_token()` mints fresh 24-hour JWTs on
# demand from CLIENT_ID + CLIENT_SECRET; nothing rotates manually anymore.
#
# `ZOOMINFO_BEARER_TOKEN` is the **legacy** static-token path. If CLIENT_SECRET
# is set, the auth module ignores BEARER_TOKEN entirely. Kept as a break-glass
# override for ops, and to avoid breaking pre-OAuth deployments mid-rollout.
#
# `ZOOMINFO_ENRICH_URL` formerly pointed at the now-deprecated legacy
# `/enrich/contact`. The current codebase calls the new Data API
# (`/gtm/data/v1/contacts/*`) directly; the var is retained for back-compat
# but no longer read by the contact-enrichment service.
ZOOMINFO_ENRICH_URL      = get_env_with_default("ZOOMINFO_ENRICH_URL",      "https://api.zoominfo.com/enrich/contact")
ZOOMINFO_BEARER_TOKEN    = get_env_with_default("ZOOMINFO_BEARER_TOKEN",    "")
ZOOMINFO_CLIENT_ID       = get_env_with_default("ZOOMINFO_CLIENT_ID",       "")
ZOOMINFO_CLIENT_SECRET   = get_env_with_default("ZOOMINFO_CLIENT_SECRET",   "")
ZOOMINFO_OAUTH_TOKEN_URL = get_env_with_default("ZOOMINFO_OAUTH_TOKEN_URL", "https://okta-login.zoominfo.com/oauth2/default/v1/token")
ZOOMINFO_SCOPES          = get_env_with_default("ZOOMINFO_SCOPES",          "api:data:contact")

# ---- Apollo Contact Enrichment ----
# Fallback provider when ZoomInfo returns no match. Empty => routes that call
# Apollo will short-circuit with a logged warning.
APOLLO_API_KEY = get_env_with_default("APOLLO_API_KEY", "")

# ---- Amplitude Telemetry ----
AMPLITUDE_API_KEY = get_env_with_default("AMPLITUDE_API_KEY", "")
AMPLITUDE_API_URL = get_env_with_default("AMPLITUDE_API_URL", "https://api2.amplitude.com/2/httpapi")
AMPLITUDE_TRACK_LOGS = get_env_bool("AMPLITUDE_TRACK_LOGS", True)

# ---- Candidate Scoring Calibration ----
# These knobs tune how harsh the match_score curve is. Defaults were calibrated
# after observing that no candidate scored above ~60% in real searches. Lower
# the cosmetic severity by rebalancing required vs preferred and softening the
# year/recent/exclusion multipliers. All values are multiplicative ratios
# applied inside `_score_candidate` / `_term_group_score` in
# services/unified_candidate_search.py.

# T1: required-vs-preferred split inside a dimension that has both groups.
# Sum should be 1.0. Old values were 0.75 / 0.25, then 0.60 / 0.40.
# Rebalanced again after observing strong candidates (82% on Skills) getting
# dragged to ~63% overall by arithmetic-mean penalties on Education/Keywords.
SCORING_REQUIRED_WEIGHT = float(get_env_with_default("SCORING_REQUIRED_WEIGHT", "0.55"))
SCORING_PREFERRED_WEIGHT = float(get_env_with_default("SCORING_PREFERRED_WEIGHT", "0.45"))

# L4: Floor for unmatched groups inside the weighted-mean ratio.
# Previously a required group that didn't match contributed 0 to the numerator,
# which turned "matched 3 of 5 strong rubric items" into a 60% ratio. The floor
# says: even a miss is worth *something*, because many "misses" are either
# synonym-matching failures (e.g. the rubric had "CS degree" and "Engineering
# degree" as separate items and the candidate has one) or parsing gaps.
# Required floor kept conservative so clear non-fits still read as non-fits.
SCORING_UNMATCHED_REQUIRED_FLOOR = float(get_env_with_default("SCORING_UNMATCHED_REQUIRED_FLOOR", "0.35"))
SCORING_UNMATCHED_PREFERRED_FLOOR = float(get_env_with_default("SCORING_UNMATCHED_PREFERRED_FLOOR", "0.25"))

# L4: Parsing-gap rescue. When the structured collections for a dimension are
# entirely empty on the candidate profile (e.g. LinkedIn extraction never
# populated `companies`) but the candidate has resume_text, the dimension's
# base_ratio floors at this value. This stops "couldn't parse their companies"
# from torpedoing a 45-weight dimension.
SCORING_PARSING_GAP_FLOOR = float(get_env_with_default("SCORING_PARSING_GAP_FLOOR", "0.65"))

# L5: Coverage-based quality lift. When a candidate hits at least this fraction
# of the groups in a dimension with good quality (fuzzy >= 0.5), the ratio
# blends toward the mean of hits-only — so a strong partial matcher isn't
# dragged down by misses that are often rubric redundancies (e.g. "CS degree"
# + "Engineering degree" listed as two separate required items when either
# satisfies the recruiter). Candidates with weak coverage fall through to the
# ordinary floored-mean, so weak fits still read as weak.
SCORING_COVERAGE_BLEND_THRESHOLD = float(get_env_with_default("SCORING_COVERAGE_BLEND_THRESHOLD", "0.5"))

# T2: per-group multipliers inside `_term_group_score`.
#  - _UNKNOWN_MULT: applied when min_years > 0 but years_of_experience didn't parse.
#  - _FLOOR:        applied as `max(FLOOR, years/min_years)` when years < min_years.
#  - _RECENT_PENALTY: applied when a group is marked "recent" but term not in recent_text.
SCORING_YEARS_UNKNOWN_MULT = float(get_env_with_default("SCORING_YEARS_UNKNOWN_MULT", "0.90"))
SCORING_YEARS_FLOOR = float(get_env_with_default("SCORING_YEARS_FLOOR", "0.55"))
SCORING_RECENT_PENALTY = float(get_env_with_default("SCORING_RECENT_PENALTY", "0.92"))

# T3: exclusion penalty cap.
#   penalty = min(total_weight * _CAP, N_hits * max(4.0, total_weight * _PER_HIT))
SCORING_EXCLUSION_CAP = float(get_env_with_default("SCORING_EXCLUSION_CAP", "0.35"))
SCORING_EXCLUSION_PER_HIT = float(get_env_with_default("SCORING_EXCLUSION_PER_HIT", "0.15"))

# Exclusion hard-veto. When `_term_group_score` for any excluded group hits
# at or above this threshold, the candidate's final match_score is forced
# to 0 — the soft penalty above is not enough on its own (a strong skill
# match elsewhere can still drag the candidate to 60+ even when they fail
# a recruiter's "no" rule). Set to 1.01 to disable.
SCORING_EXCLUSION_HARD_VETO_THRESHOLD = float(
    get_env_with_default("SCORING_EXCLUSION_HARD_VETO_THRESHOLD", "0.85")
)

# Additive source-tier bonus applied to match_score in `finalize_candidate`.
# Warm leads (recruiter's own applicants) and curated pools should rank
# above cold scrapes when raw scores are close. Bonus is only applied when
# the base score is non-zero, so excluded/no-fit candidates aren't
# promoted. Set any tier to 0 to disable that tier's boost.
SOURCE_TIER_BONUS = {
    "JobDiva-Applicants": int(
        get_env_with_default("SOURCE_TIER_BONUS_JOBDIVA_APPLICANTS", "10")
    ),
    # Refactor `b5a6aaa` switched JobDiva talent-pool sourcing to
    # JobAgent-only and renamed the source label from JobDiva-TalentSearch
    # → JobDiva-JobAgent. The old key is kept for any in-flight records that
    # still carry the legacy label; the new key picks up the same default
    # bonus (raised from +5 to +10 because JobAgent is now the *only* way
    # JobDiva candidates reach the funnel, and they're already pre-ranked
    # for relevance by JobDiva's own matcher — see api_rank floor in
    # finalize_candidate).
    "JobDiva-JobAgent": int(
        get_env_with_default("SOURCE_TIER_BONUS_JOBDIVA_JOBAGENT", "10")
    ),
    "JobDiva-TalentSearch": int(
        get_env_with_default("SOURCE_TIER_BONUS_JOBDIVA_TALENT", "5")
    ),
    "JobDiva": int(
        get_env_with_default("SOURCE_TIER_BONUS_JOBDIVA_TALENT", "5")
    ),
    "VettedDB": int(get_env_with_default("SOURCE_TIER_BONUS_VETTED", "2")),
}

# JobAgent rank → match_score floor. JobDiva's JobAgent returns candidates
# ranked by their own relevance matcher; api_rank is 0-based, with 0 = top
# pick. Recruiters trust JobDiva's ranking — our rubric-literal-match
# scorer was overriding that trust with low scores when the resume happened
# to use synonyms (rubric "MS Office Suite" vs resume "Microsoft Office").
# This floor ensures JobDiva's top picks never score below what their rank
# implies. The rubric score still applies on top of the floor — a top-5
# candidate who is *also* a strong rubric match scores higher, not lower.
#
# Tuning notes:
#  - Floors below 50 are deliberately omitted so deep-tail candidates
#    (api_rank > 29) fall back entirely on the rubric score.
#  - The source-tier bonus runs AFTER the floor, so a JobDiva top-5 lands
#    at min 85 + 10 (JobAgent bonus) = 95.
JOBAGENT_RANK_SCORE_FLOOR = (
    (5, 85),   # api_rank 0..4   (top 5)
    (10, 75),  # api_rank 5..9   (next 5)
    (20, 65),  # api_rank 10..19
    (30, 55),  # api_rank 20..29
)

# Embedding-based skill matching (PR-C). Off by default — when enabled,
# `_fuzzy_term_score` augments its keyword-overlap signal with the cosine
# similarity between query-term and candidate-skill embeddings, taking
# whichever is higher. Designed to recover phrasing variants the keyword
# path misses ("React" vs "ReactJS", "Web Dev" vs "Web Development").
#
# Embeddings are pre-warmed per-candidate and per-search via the
# `services.skill_embeddings` module's in-process LRU cache, so a typical
# search incurs O(unique_skills) embedding API calls regardless of the
# number of scoring loops.
EMBEDDING_SKILL_MATCH = get_env_bool("EMBEDDING_SKILL_MATCH", False)
EMBEDDING_MATCH_THRESHOLD = float(
    get_env_with_default("EMBEDDING_MATCH_THRESHOLD", "0.75")
)
OPENAI_EMBEDDING_MODEL = get_env_with_default(
    "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
)
EMBEDDING_CACHE_MAX = int(get_env_with_default("EMBEDDING_CACHE_MAX", "50000"))

# Per-family override for the embedding skill matcher. Keyword fuzzy
# matching misses non-IT synonymy ("Stakeholder Management" ↔ "Executive
# Alignment", "Pipeline Forecasting" ↔ "Quota Attainment Planning"), so
# we turn embeddings on for non-IT families while keeping the legacy IT
# default (`EMBEDDING_SKILL_MATCH`, off unless explicitly enabled).
# Lookup order in the scorer: family-specific override → global flag.
EMBEDDING_SKILL_MATCH_BY_FAMILY = {
    "program_management": True,
    "sales": True,
    "hr": True,
    "marketing": True,
    "customer_success": True,
    "finance": True,
    "accounting": True,
    "healthcare": True,
    "legal": True,
    "education": True,
    "ops": True,
    "recruiting": True,
    "generic_non_it": True,
}


# ---- Role-family-aware scoring weights ----
# Default scoring path (used for IT and unknown family) is preserved
# byte-for-byte from the pre-fix configuration — IT recruiters do not
# get a different score on the same JD/candidate after this change.
# Non-IT families use a rebalance that down-weights skills (which are
# often a thin signal for these roles, since the rubric typically has
# 2-4 skills) and up-weights titles + experience trajectory + certs/
# licenses (CPA / CFA / Bar / RN).
SCORING_WEIGHTS_DEFAULT = {
    "titles": 15.0,
    "skills": 45.0,
    "location": 4.0,
    "companies": 5.0,
    "education": 8.0,
    "certifications": 7.0,
    "keywords": 5.0,
}

# Non-IT weight sets. Each must sum to the same total as the default
# (89.0) so an apples-to-apples score can be compared across families.
SCORING_WEIGHTS_BY_FAMILY = {
    "program_management": {
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 8.0, "certifications": 7.0, "keywords": 9.0,
    },
    "sales": {
        # Book-of-business signal lives in "company experience" for sales —
        # the candidate's prior employers are themselves a quota signal.
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 8.0,
        "education": 7.0, "certifications": 6.0, "keywords": 9.0,
    },
    "hr": {
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 8.0, "certifications": 7.0, "keywords": 9.0,
    },
    "marketing": {
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 8.0, "certifications": 7.0, "keywords": 9.0,
    },
    "customer_success": {
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 8.0, "certifications": 7.0, "keywords": 9.0,
    },
    "finance": {
        # Cert-heavy: CPA / CFA / FRM signal is genuine.
        "titles": 20.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 10.0, "certifications": 10.0, "keywords": 9.0,
    },
    "accounting": {
        "titles": 20.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 10.0, "certifications": 10.0, "keywords": 9.0,
    },
    "ops": {
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 8.0, "certifications": 7.0, "keywords": 9.0,
    },
    "recruiting": {
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 8.0, "certifications": 7.0, "keywords": 9.0,
    },
    "healthcare": {
        # License-heavy: RN / NP / PA / state license signal dominates.
        "titles": 20.0, "skills": 30.0, "location": 4.0, "companies": 4.0,
        "education": 12.0, "certifications": 15.0, "keywords": 4.0,
    },
    "legal": {
        # Bar admission and jurisdiction signal dominates.
        "titles": 20.0, "skills": 30.0, "location": 4.0, "companies": 4.0,
        "education": 12.0, "certifications": 15.0, "keywords": 4.0,
    },
    "education": {
        "titles": 20.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 12.0, "certifications": 8.0, "keywords": 9.0,
    },
    "generic_non_it": {
        "titles": 25.0, "skills": 30.0, "location": 4.0, "companies": 6.0,
        "education": 8.0, "certifications": 7.0, "keywords": 9.0,
    },
}


def scoring_weights_for_family(family: Optional[str] = None) -> dict:
    """Return the dimension-weight map for a family.

    `None` and `"it"` return the legacy default — preserves IT scoring
    behavior byte-for-byte. Unknown families fall back to default rather
    than to `generic_non_it`, since unknown often means the classifier
    couldn't read the title (e.g. blank string).
    """
    if family is None or family == "it":
        return SCORING_WEIGHTS_DEFAULT
    return SCORING_WEIGHTS_BY_FAMILY.get(family, SCORING_WEIGHTS_DEFAULT)


def embedding_skill_match_for_family(family: Optional[str] = None) -> bool:
    """Resolve the effective embedding-skill-match flag for a family.

    Order of precedence: family-specific override > global flag. IT and
    unknown families read the global flag, so the existing
    `EMBEDDING_SKILL_MATCH` env var still controls them.
    """
    if family in EMBEDDING_SKILL_MATCH_BY_FAMILY:
        return EMBEDDING_SKILL_MATCH_BY_FAMILY[family]
    return EMBEDDING_SKILL_MATCH
