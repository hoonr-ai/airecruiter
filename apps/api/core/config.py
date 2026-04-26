import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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

# ---- Encryption ----
ENCRYPTION_KEY = get_env_or_fail("ENCRYPTION_KEY")
ENCRYPTION_SALT = os.getenv("ENCRYPTION_SALT")

# ---- Azure AI Agent (skill-role-extractor) ----
AZURE_AI_PROJECT_ENDPOINT = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "")
AZURE_OPENAI_API_KEY      = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_AI_AGENT_NAME       = os.getenv("AZURE_AI_AGENT_NAME", "skill-role-extractor")

# ---- Exa API ----
EXA_API_KEY = get_env_with_default("EXA_API_KEY", "")

# ---- ZoomInfo Contact Enrichment ----
ZOOMINFO_ENRICH_URL = get_env_with_default("ZOOMINFO_ENRICH_URL", "https://api.zoominfo.com/enrich/contact")
ZOOMINFO_BEARER_TOKEN = get_env_with_default("ZOOMINFO_BEARER_TOKEN", "")
ZOOMINFO_CLIENT_ID = get_env_with_default("ZOOMINFO_CLIENT_ID", "")

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
