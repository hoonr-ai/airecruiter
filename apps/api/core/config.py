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
