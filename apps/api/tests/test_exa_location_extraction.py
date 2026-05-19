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
    OPENAI_API_KEY="",
    GEMINI_API_KEY="",
)

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
    # 6. LinkedIn "Greater <City> Area" header pattern.
    (
        "Greater Boston Area · Senior Data Scientist · 6+ years machine learning.",
        ("Boston", ""),
    ),
    # 7. "<City>, <State> Area" LinkedIn pattern.
    (
        "Atlanta, Georgia Area | Vice President, Product",
        ("Atlanta", "GA"),
    ),
    # 8. Negative: city alone with no state and no Area suffix → empty.
    (
        "Resume highlights: 10 years Java experience. Strong AWS skills.",
        ("", ""),
    ),
    # 9. Negative: non-US country should NOT be returned as a US state.
    (
        "Located in Bangalore, India. Senior backend engineer with 9 years.",
        ("", ""),
    ),
    # 10. "Resides in CITY, ST" — newly supported verb.
    (
        "Resides in Miami, FL. Bilingual sales leader.",
        ("Miami", "FL"),
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
