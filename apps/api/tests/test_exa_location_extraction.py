"""Tests for the Exa highlight → (city, state) extractor.

Standalone script: no pytest dependency. Run with:
    cd apps/api && python -m tests.test_exa_location_extraction

Backstops the F4 widen in services/exa_service.py:_extract_city_from_highlights.
Each CASE is a real-shaped Exa highlight body. EXPECTED is (city, state_code).
Empty strings mean "no confident extraction" — used for negative cases.
"""

import sys
import os
import types

# Make the parent package importable when the test is run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub out heavy production deps so the test runs without dotenv/exa_py/openai
# and without requiring real env vars. We only need the pure-function regex
# helpers, not the API clients.
def _stub_module(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_stub_module("dotenv", load_dotenv=lambda *a, **k: None)
_stub_module("exa_py", Exa=object)
_stub_module("httpx")
_stub_module("openai", AsyncOpenAI=object)


class _StubBaseModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


_stub_module("pydantic", BaseModel=_StubBaseModel, Field=lambda *a, **k: None)
# Stub core.config so we don't trip the env-var-required guard at import time.
_stub_module("core", __path__=[])
_stub_module(
    "core.config",
    EXA_API_KEY="",
    EXA_CONTACT_ENRICH_ENABLED=True,
    OPENAI_API_KEY="",
    GEMINI_API_KEY="",
)
# Stub the LLM client singleton — services/location.py imports from it
# transitively. We only exercise pure regex helpers here, so any object
# that exposes the two used names is fine.
_stub_module(
    "core.llm_client",
    get_openai_client=lambda: None,
    model_for=lambda purpose, default: default,
)
_stub_module("core.llm_cache", make_key=lambda *a, **kw: "stub", get_json=lambda k: None, set_json=lambda *a, **kw: None, get_str=lambda k: None, set_str=lambda *a, **kw: None)

from services.exa_service import _extract_city_from_highlights  # noqa: E402


CASES = [
    # 1. Classic "Located in CITY, ST" — the only form the old regex caught.
    (
        "Senior Program Manager. Located in Denver, CO. 12 years of experience.",
        ("Denver", "CO"),
    ),
    # 2. "Based in CITY, ST" — newly supported verb.
    (
        "Software architect based in Austin, TX with 8 years of cloud expertise.",
        ("Austin", "TX"),
    ),
    # 3. "Currently in CITY, ST" — newly supported.
    (
        "Currently in Seattle, WA. Open to remote roles.",
        ("Seattle", "WA"),
    ),
    # 4. Bare "City, ST" in the first 400 chars — within widened head window.
    (
        "Jane Doe - Product Manager. Plano, TX. Background in healthcare PM, "
        "agile, scrum. Last role was at a Fortune 500 insurer leading a "
        "cross-functional team of 12. Active LinkedIn member with 500+ "
        "connections in the DFW metro area.",
        ("Plano", "TX"),
    ),
    # 5. Full state name normalised to code: "Dallas, Texas" → ("Dallas", "TX").
    (
        "Director of Engineering — Dallas, Texas. Built three platforms.",
        ("Dallas", "TX"),
    ),
    # 6. LinkedIn "Greater <City> Area" header pattern — full label is preserved.
    (
        "Greater Boston Area · Senior Data Scientist · 6+ years machine learning.",
        ("Greater Boston Area", ""),
    ),
    # 7. "<City>, <State> Area" LinkedIn pattern — state IS resolvable → (city, code).
    (
        "Atlanta, Georgia Area | Vice President, Product",
        ("Atlanta", "GA"),
    ),
    # 8. Negative: city alone with no state and no Area suffix → empty.
    (
        "Resume highlights: 10 years Java experience. Strong AWS skills.",
        ("", ""),
    ),
    # 9. Non-US country is kept as a place so the country gate can drop it.
    # Returning empty used to show "Location Unavailable" and soft-keep India.
    (
        "Located in Bangalore, India. Senior backend engineer with 9 years.",
        ("Bangalore, India", ""),
    ),
    # 9b. Hyphenated metro name is not truncated at the hyphen.
    (
        "Md Hasanul Azaz Aman. Dallas-Fort Worth Metroplex",
        ("Dallas-Fort Worth Metroplex", ""),
    ),
    # 9c. "City, State, Country" keeps the US city and state.
    (
        "Location: Edison, New Jersey, United States",
        ("Edison", "NJ"),
    ),
    # A job city earlier in the snippet must not hide the profile location.
    (
        "Senior engineer, Austin, TX. Location: Edison, New Jersey, United States",
        ("Edison", "NJ"),
    ),
    # Bare profile city: show it. Edison is in several states, so no state
    # is invented. Tempe and Secaucus exist in one state.
    (
        "Location: Edison",
        ("Edison", ""),
    ),
    (
        "Location: Tempe",
        ("Tempe", "AZ"),
    ),
    (
        "Location: Secaucus",
        ("Secaucus", "NJ"),
    ),
    (
        "Location: Hyderabad\nJagadeesh - Hyderabad, Telangana, India | LinkedIn",
        ("Hyderabad, India", ""),
    ),
    # 9d. "City, Region, Country" keeps the country for the outside-country gate.
    (
        "Jagadeesh Rameswarapu - Hyderabad, Telangana, India | Professional Profile",
        ("Hyderabad, India", ""),
    ),
    # 10. "Resides in CITY, ST" — newly supported verb.
    (
        "Resides in Miami, FL. Bilingual sales leader.",
        ("Miami", "FL"),
    ),
    # 11. Ignore a headline technology brand + state-code-looking suffix and
    # continue to the actual LinkedIn location header.
    # Full "Greater Chicago Area" label preserved for display.
    (
        "John Lash - Salesforce, MS Dynamics. Greater Chicago Area",
        ("Greater Chicago Area", ""),
    ),
    # 12. Ignore a two-letter resume fragment that resembles a city, then
    # recover the broad LinkedIn metro location. Full label preserved.
    (
        "Yuhong Ouyang - PS, PR. Los Angeles Metropolitan Area",
        ("Los Angeles Metropolitan Area", ""),
    ),
    # 13. Devlin Rocha / Laura Wood scenario: Bay Area label is preserved so the
    # candidate card shows "San Francisco Bay Area" instead of being blank.
    (
        "Devlin Rocha - Software Engineer. San Francisco Bay Area",
        ("San Francisco Bay Area", ""),
    ),
    # 14. Multi-word city names: Greater prefix + Bay suffix both preserved.
    (
        "Senior Product Manager · Greater San Francisco Bay Area",
        ("Greater San Francisco Bay Area", ""),
    ),
    # 15. Laura Wood scenario: exact LinkedIn header string.
    (
        "Laura Wood - San Jose State University - San Francisco Bay Area",
        ("San Francisco Bay Area", ""),
    ),
]


def run() -> int:
    failures = []
    for idx, (text, expected) in enumerate(CASES, start=1):
        got = _extract_city_from_highlights(text)
        if got != expected:
            failures.append((idx, text[:60], expected, got))

    if failures:
        print(f"FAIL: {len(failures)}/{len(CASES)} cases failed")
        for idx, snippet, expected, got in failures:
            print(f"  case {idx}: text={snippet!r}")
            print(f"    expected={expected}  got={got}")
        return 1

    print(f"OK: all {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
