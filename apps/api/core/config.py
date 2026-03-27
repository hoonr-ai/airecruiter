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

# ---- Required Variables ----
OPENAI_API_KEY = get_env_or_fail("OPENAI_API_KEY")
DATABASE_URL = get_env_or_fail("DATABASE_URL")
GEMINI_API_KEY = get_env_or_fail("GEMINI_API_KEY")

# JobDiva Configuration
JOBDIVA_API_URL = get_env_with_default("JOBDIVA_API_URL", "https://api.jobdiva.com")
JOBDIVA_CLIENT_ID = get_env_or_fail("JOBDIVA_CLIENT_ID")
JOBDIVA_USERNAME = get_env_or_fail("JOBDIVA_USERNAME")
JOBDIVA_PASSWORD = get_env_or_fail("JOBDIVA_PASSWORD")

# Unipile Configuration
UNIPILE_API_KEY = get_env_or_fail("UNIPILE_API_KEY")
UNIPILE_DSN = get_env_with_default("UNIPILE_DSN", "api1.unipile.com")
UNIPILE_ACCOUNT_ID = get_env_or_fail("UNIPILE_ACCOUNT_ID")

# ---- Database Settings (Legacy/Specific scripts) ----
DB_USER = get_env_with_default("DB_USER", "postgres")
DB_PASSWORD = get_env_with_default("DB_PASSWORD", "password")
DB_NAME = get_env_with_default("DB_NAME", "postgres")
CLOUDSQL_CONNECTION_NAME = os.getenv("CLOUDSQL_CONNECTION_NAME")

# ---- UDF Mappings ----
JOBDIVA_AI_JD_UDF_ID = int(get_env_with_default("JOBDIVA_AI_JD_UDF_ID", "230"))
JOBDIVA_JOB_NOTES_UDF_ID = int(get_env_with_default("JOBDIVA_JOB_NOTES_UDF_ID", "231"))

# ---- Encryption ----
ENCRYPTION_KEY = get_env_or_fail("ENCRYPTION_KEY")
ENCRYPTION_SALT = get_env_or_fail("ENCRYPTION_SALT")
