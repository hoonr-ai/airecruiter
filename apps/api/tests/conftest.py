"""Shared pytest bootstrap for apps/api.

Router imports pull in core.config, which requires JobDiva/OpenAI env vars at
import time. CI test-api does not have production secrets, so stub them here
before any test module imports application code.
"""
import os

for _key in (
    "OPENAI_API_KEY",
    "JOBDIVA_CLIENT_ID",
    "JOBDIVA_USERNAME",
    "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY",
    "UNIPILE_ACCOUNT_ID",
    "ENCRYPTION_KEY",
):
    os.environ.setdefault(_key, "test")

os.environ.setdefault("DATABASE_URL", "sqlite://")
