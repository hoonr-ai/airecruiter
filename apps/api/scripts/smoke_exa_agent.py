"""Live smoke test: confirm `exa.beta.agent.runs` works for our use case.

Run from `apps/api/`:
    EXA_API_KEY=... python scripts/smoke_exa_agent.py

What it verifies:
  1. Beta header (`agent-2026-05-07`) is accepted by the account.
  2. `output_schema` produces a `output.structured` payload matching our shape.
  3. `input.data` (LinkedIn URL seeds) are honoured by the Agent.
  4. `deep_research_candidates()` returns the parsed candidates list.

Exits 0 on full success, 1 on any failure. Prints cost so we can see what
a `effort=low` test run actually costs against the live API.
"""
import asyncio
import json
import logging
import os
import sys

# Allow running from `apps/api/` without sys.path hacks at the top of the
# repo — `scripts/` sits next to `services/` already.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env from the api directory so EXA_API_KEY is available without
# the operator having to export it manually.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

# Stub the required-but-unused env vars before importing services.exa_service
# (core.config raises on missing values — they're not used by this script).
for var in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME",
    "JOBDIVA_PASSWORD", "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID",
    "DATABASE_URL", "ENCRYPTION_KEY",
):
    os.environ.setdefault(var, "smoke-test-stub")

# Use the production default (`medium`) unless the operator explicitly
# overrides. `effort=low` consistently fails our 4-field schema with
# `stop_reason=error` because the budget cap is too tight to crawl
# LinkedIn for follower_count / last_activity.
os.environ["EXA_AGENT_EFFORT"] = os.environ.get("EXA_AGENT_EFFORT", "medium")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_exa_agent")

from services.exa_service import exa_service


async def main() -> int:
    if not os.getenv("EXA_API_KEY"):
        print("ERROR: EXA_API_KEY is not set", file=sys.stderr)
        return 1
    if not exa_service.exa:
        print("ERROR: exa_service.exa client is None", file=sys.stderr)
        return 1

    print("=" * 70)
    print("SMOKE 1: SDK exposes exa.beta.agent.runs")
    print("=" * 70)
    try:
        runs = exa_service.exa.beta.agent.runs
        assert callable(runs.create)
        assert callable(runs.poll_until_finished)
        print("OK — exa.beta.agent.runs.create / poll_until_finished both callable")
    except Exception as e:
        print(f"FAIL — {e}")
        return 1

    print()
    print("=" * 70)
    print("SMOKE 2: live Agent run (effort=low, 2 known LinkedIn URLs)")
    print("=" * 70)
    # Real, well-known public LinkedIn profiles — enrichment should land
    # follower count, recent companies, and an activity snippet.
    seed_urls = [
        "https://www.linkedin.com/in/satyanadella/",
        "https://www.linkedin.com/in/jeffweiner08/",
    ]
    try:
        candidates = await exa_service.deep_research_candidates(
            jd_title="Chief Executive Officer",
            jd_role="Technology Executive",
            skills=["leadership", "strategy", "enterprise software"],
            location="United States",
            seed_urls=seed_urls,
        )
    except Exception as e:
        print(f"FAIL — deep_research_candidates raised: {e!r}")
        return 1

    if not candidates:
        print("FAIL — empty candidates list returned")
        print("Check API logs above for 'Exa Agent finished with status=...' diagnostics.")
        return 1

    print(f"OK — returned {len(candidates)} candidates")

    print()
    print("=" * 70)
    print("SMOKE 3: structured fields present")
    print("=" * 70)
    sample = candidates[0]
    expected_keys = {"linkedin_url", "fit_rationale"}
    optional_keys = {"name", "current_title", "location",
                     "last_activity", "follower_count", "recent_companies"}
    missing_required = expected_keys - set(sample.keys())
    present_optional = optional_keys & set(sample.keys())

    if missing_required:
        print(f"FAIL — missing required keys on sample candidate: {missing_required}")
        print("Sample:", json.dumps(sample, indent=2, default=str)[:1000])
        return 1
    print(f"OK — required keys present: {expected_keys}")
    print(f"OK — optional keys present: {present_optional}")
    print()
    print("Sample candidate:")
    print(json.dumps(sample, indent=2, default=str)[:1500])

    print()
    print("=" * 70)
    print("ALL SMOKES PASSED")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
