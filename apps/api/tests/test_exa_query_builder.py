"""Tests for F1: Exa query builder no longer hardcodes a role hint.

Standalone script — same stubbing strategy as test_exa_location_extraction.
Run with:
    cd apps/api && python -m tests.test_exa_query_builder
"""

import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
_stub_module("core", __path__=[])
_stub_module("core.config", EXA_API_KEY="", OPENAI_API_KEY="", GEMINI_API_KEY="")
_stub_module(
    "core.llm_client",
    get_openai_client=lambda: None,
    model_for=lambda purpose, default: default,
)
_stub_module(
    "core.llm_cache",
    make_key=lambda *a, **kw: "stub",
    get_json=lambda k: None,
    set_json=lambda *a, **kw: None,
    get_str=lambda k: None,
    set_str=lambda *a, **kw: None,
)

from services.exa_service import _exa_query_from_boolean  # noqa: E402


def _check(label: str, got: str, must_contain: list, must_not_contain: list) -> bool:
    ok = True
    for needle in must_contain:
        if needle.lower() not in got.lower():
            print(f"FAIL [{label}]: expected to contain {needle!r}; got {got!r}")
            ok = False
    for needle in must_not_contain:
        if needle.lower() in got.lower():
            print(f"FAIL [{label}]: expected NOT to contain {needle!r}; got {got!r}")
            ok = False
    return ok


def run() -> int:
    failures = 0

    # Case 1: boolean_string empty, role_hint set from title criteria.
    # The query must surface "Program Manager" and must NOT reintroduce
    # the old hardcoded "software engineer OR developer" anchor.
    q = _exa_query_from_boolean(
        boolean_string="",
        skills=["agile", "scrum"],
        location="Denver, CO",
        role_hint='"Program Manager"',
    )
    if not _check(
        "role_hint anchors fallback query",
        q,
        must_contain=["Program Manager", "agile", "Denver, CO"],
        must_not_contain=["software engineer", "developer"],
    ):
        failures += 1

    # Case 2: boolean_string empty AND role_hint empty → defaults to "candidate"
    # (not "software engineer OR developer").
    q = _exa_query_from_boolean(
        boolean_string="",
        skills=["python"],
        location="Austin, TX",
        role_hint="",
    )
    if not _check(
        "empty role_hint defaults to 'candidate'",
        q,
        must_contain=["candidate", "python", "Austin, TX"],
        must_not_contain=["software engineer", "developer"],
    ):
        failures += 1

    # Case 3: boolean_string provided → it's used verbatim (radius stripped),
    # role_hint is ignored. This is the dominant path when Step-5 has
    # structured filters.
    q = _exa_query_from_boolean(
        boolean_string='"Program Manager" AND "agile" AND "Denver, CO"',
        skills=["python"],
        location="Denver, CO",
        role_hint='"Should Not Appear"',
    )
    if not _check(
        "boolean_string supersedes role_hint",
        q,
        must_contain=["Program Manager", "agile", "Denver, CO"],
        must_not_contain=["Should Not Appear", "software engineer"],
    ):
        failures += 1

    # Case 4: ` within N mi` radius hint inside boolean_string is stripped
    # (Exa can't act on radius).
    q = _exa_query_from_boolean(
        boolean_string='"Software Engineer" AND "Denver, CO" within 25 mi',
        skills=[],
        location="",
        role_hint="",
    )
    if not _check(
        "radius hint stripped from boolean",
        q,
        must_contain=["Software Engineer", "Denver, CO"],
        must_not_contain=["within 25 mi", "within25mi"],
    ):
        failures += 1

    if failures:
        print(f"FAIL: {failures} case(s) failed")
        return 1
    print("OK: all 4 cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
