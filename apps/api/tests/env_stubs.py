"""Import-time env stubs for core.config get_env_or_fail keys.

Keep in sync with get_env_or_fail(...) calls in core/config.py.
Used by tests/conftest.py (pytest) and standalone test modules.
"""
import os

# 32-byte hex key for utils/crypto.py bytes.fromhex(ENCRYPTION_KEY)
_STUB_ENCRYPTION_KEY_HEX = "0" * 64
_STUB_DATABASE_URL = "postgresql://test:test@localhost:5432/test"

_REQUIRED_ENV = {
    "OPENAI_API_KEY": "test",
    "JOBDIVA_CLIENT_ID": "test",
    "JOBDIVA_USERNAME": "test",
    "JOBDIVA_PASSWORD": "test",
    "DATABASE_URL": _STUB_DATABASE_URL,
    "ENCRYPTION_KEY": _STUB_ENCRYPTION_KEY_HEX,
}


def stub_required_env() -> None:
    """Stub config.py required env vars before application imports."""
    for key, value in _REQUIRED_ENV.items():
        os.environ.setdefault(key, value)
