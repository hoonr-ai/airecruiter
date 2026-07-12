"""Tests for the natural-language Exa query builder.

Exa Search does not parse boolean/ATS syntax (confirmed by Exa engineering,
2026-07), so queries must be prose sentences, one role per search. These
tests pin:
  - one query per title, capped, each a boolean-free sentence
  - the engineer-recommended shape: role + years + skills + location
  - fallbacks: legacy role_hint ('"A" OR "B"'), flattened boolean string,
    skills-only

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
_stub_module(
    "core.config",
    EXA_API_KEY="",
    EXA_CONTACT_ENRICH_ENABLED=True,
    OPENAI_API_KEY="",
    GEMINI_API_KEY="",
)
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

from services.exa_service import (  # noqa: E402
    ExaService,
    _boolean_to_terms,
    build_deep_research_output_schema,
    build_people_queries,
    compose_people_query,
)

# Boolean/ATS artifacts that must never leak into an Exa query. Checked
# CASE-SENSITIVELY: boolean operators are uppercase; the lowercase prose
# 'and' in e.g. 'Autosys and performance tuning experience' is exactly the
# natural language we want.
_BOOLEAN_ARTIFACTS = [" AND ", " OR ", " NOT ", '"', "(", ")"]


def _check(label: str, got: str, must_contain: list, must_not_contain: list) -> bool:
    ok = True
    for needle in must_contain:
        if needle.lower() not in got.lower():
            print(f"FAIL [{label}]: expected to contain {needle!r}; got {got!r}")
            ok = False
    for needle in must_not_contain:
        case_sensitive = needle in _BOOLEAN_ARTIFACTS
        hit = (needle in got) if case_sensitive else (needle.lower() in got.lower())
        if hit:
            print(f"FAIL [{label}]: expected NOT to contain {needle!r}; got {got!r}")
            ok = False
    return ok


def run() -> int:
    failures = 0

    # Case 1: the engineer-recommended shape — role + years + skills +
    # location composed as one prose sentence, no boolean artifacts.
    q = compose_people_query(
        "Senior Oracle PL/SQL Developer",
        skills=["Autosys", "performance tuning"],
        location="Jersey City, NJ, United States",
        min_experience_years=5,
    )
    if not _check(
        "engineer-recommended sentence shape",
        q,
        must_contain=[
            "Senior Oracle PL/SQL Developer",
            "5+ years",
            "Autosys",
            "performance tuning",
            "based in Jersey City, NJ",
        ],
        must_not_contain=_BOOLEAN_ARTIFACTS,
    ):
        failures += 1

    # Case 2: skills already named in the role title are not repeated.
    q = compose_people_query(
        "Senior Oracle PL/SQL Developer",
        skills=["PL/SQL", "Autosys"],
        location="Austin, TX",
    )
    if q.lower().count("pl/sql") != 1:
        print(f"FAIL [skill-in-title dedupe]: expected exactly one 'PL/SQL'; got {q!r}")
        failures += 1

    # Case 3: one query per title, capped at 3, each carrying its own role
    # plus the shared skills/location.
    queries = build_people_queries(
        titles=["Program Manager", "Project Manager", "Delivery Manager", "Scrum Master"],
        skills=["agile"],
        location="Denver, CO, United States",
    )
    if len(queries) != 3:
        print(f"FAIL [one role per search]: expected 3 queries; got {len(queries)}: {queries!r}")
        failures += 1
    for role, q in zip(["Program Manager", "Project Manager", "Delivery Manager"], queries):
        if not _check(
            f"per-role query ({role})",
            q,
            must_contain=[role, "agile", "Denver, CO"],
            must_not_contain=_BOOLEAN_ARTIFACTS,
        ):
            failures += 1

    # Case 4: legacy quoted role_hint fallback splits into one query per role.
    queries = build_people_queries(
        role_hint='"Program Manager" OR "Project Manager"',
        skills=["agile", "scrum"],
        location="Denver, CO",
    )
    if len(queries) != 2:
        print(f"FAIL [role_hint fallback]: expected 2 queries; got {queries!r}")
        failures += 1
    else:
        for role, q in zip(["Program Manager", "Project Manager"], queries):
            if not _check(
                f"role_hint query ({role})",
                q,
                must_contain=[role, "agile", "Denver, CO"],
                must_not_contain=_BOOLEAN_ARTIFACTS,
            ):
                failures += 1

    # Case 5: boolean flattening — operators, parens, quotes, radius hints
    # and NOT-groups are all stripped; exclusion terms must NOT appear (Exa
    # has no negation, so they'd attract what they were meant to filter).
    terms = _boolean_to_terms(
        '"Program Manager" AND ("agile" OR "scrum") AND "Denver, CO" within 25 mi NOT ("intern" OR "student")'
    )
    joined = " | ".join(terms)
    if not _check(
        "boolean flattened to plain terms",
        joined,
        must_contain=["Program Manager", "agile", "scrum", "Denver, CO"],
        must_not_contain=["intern", "student", "within", " AND ", " OR ", '"'],
    ):
        failures += 1

    # Case 6: boolean-only fallback (no titles/skills/role_hint) still yields
    # a boolean-free natural-language query anchored on the leading term.
    queries = build_people_queries(
        boolean_string='"Software Engineer" AND "Denver, CO" within 25 mi',
        location="Denver, CO, United States",
    )
    if len(queries) != 1 or not _check(
        "boolean-only fallback",
        queries[0],
        must_contain=["Software Engineer", "based in Denver, CO"],
        must_not_contain=[*_BOOLEAN_ARTIFACTS, "within 25 mi"],
    ):
        failures += 1

    # Case 7: nothing structured at all → single generic query that still
    # anchors on skills + location and never reintroduces the old hardcoded
    # "software engineer OR developer" role bias.
    queries = build_people_queries(skills=["python"], location="Austin, TX, United States")
    if len(queries) != 1 or not _check(
        "generic fallback",
        queries[0],
        must_contain=["python", "Austin, TX"],
        must_not_contain=[*_BOOLEAN_ARTIFACTS, "software engineer", "developer"],
    ):
        failures += 1

    # Case 8: bare "United States" location reads as prose.
    q = compose_people_query("Data Engineer", location="United States")
    if not _check(
        "bare-US location",
        q,
        must_contain=["based in the United States"],
        must_not_contain=_BOOLEAN_ARTIFACTS,
    ):
        failures += 1

    # Case 9: skill-vs-role dedupe is word-boundary, not substring — 'Java'
    # must survive for a 'JavaScript Developer' (and 'Go' for Django roles).
    q = compose_people_query(
        "JavaScript Developer", skills=["Java", "Spring"], location="Austin, TX"
    )
    words = [w.strip(",.").lower() for w in q.split()]
    if "java" not in words or "spring" not in words:
        print(f"FAIL [short skill survives superstring role]: got {q!r}")
        failures += 1
    q = compose_people_query("Django Developer", skills=["Go", "AWS"])
    if "go" not in [w.strip(",.").lower() for w in q.split()]:
        print(f"FAIL [Go survives Django role]: got {q!r}")
        failures += 1

    # Case 10: a role living ONLY in a recruiter-edited boolean is still used
    # even when structured skills exist (the boolean is the only place a
    # recruiter-typed role that never became a title chip can surface).
    queries = build_people_queries(
        skills=["agile"],
        boolean_string='"Program Manager" AND "agile"',
        location="Denver, CO, United States",
    )
    if len(queries) != 1 or not _check(
        "boolean role kept alongside structured skills",
        queries[0],
        must_contain=["Program Manager", "agile", "Denver, CO"],
        must_not_contain=_BOOLEAN_ARTIFACTS,
    ):
        failures += 1

    # Case 11: keywords get their own sentence slot — a job with 5 skills
    # must not truncate the keyword away; companies read as employment.
    q = compose_people_query(
        "Senior Java Developer",
        skills=["Spring", "AWS", "Kafka", "SQL", "Docker"],
        location="Madison, WI",
        keywords=["TS/SCI clearance"],
        companies=["Epic Systems"],
    )
    if not _check(
        "keywords and companies survive 5-skill cap",
        q,
        must_contain=["TS/SCI clearance", "who has worked at Epic Systems", "Docker"],
        must_not_contain=_BOOLEAN_ARTIFACTS,
    ):
        failures += 1

    failures += run_fanout_cases()
    failures += run_schema_cases()

    if failures:
        print(f"FAIL: {failures} case(s) failed")
        return 1
    print("OK: all cases passed")
    return 0


class _FakeExa:
    """Routes search_and_contents calls by which role the query mentions."""

    def __init__(self, by_role: dict):
        self.by_role = by_role

    def search_and_contents(self, query, **kwargs):
        for role, resp in self.by_role.items():
            if role in query:
                if isinstance(resp, Exception):
                    raise resp
                return types.SimpleNamespace(results=resp)
        return types.SimpleNamespace(results=[])


def _mk_result(url: str, rid: str, title: str):
    return types.SimpleNamespace(url=url, id=rid, title=title, highlights=None)


def run_fanout_cases() -> int:
    """Pin the fan-out merge: URL dedupe across sibling role queries,
    round-robin interleave, per-query failure tolerance, cap at limit."""
    import asyncio

    failures = 0
    svc = ExaService()
    svc.exa = _FakeExa({
        "Program Manager": [
            _mk_result("https://www.linkedin.com/in/alice/", "a1", "Alice Smith - PM"),
            _mk_result("https://linkedin.com/in/bob", "b1", "Bob Jones - PM"),
        ],
        "Project Manager": [
            # Same profile as alice at a different URL spelling — must dedupe.
            _mk_result("http://linkedin.com/in/alice", "a2", "Alice Smith - PjM"),
            _mk_result("https://linkedin.com/in/carol", "c1", "Carol Lee - PjM"),
        ],
        # One role's search failing must not sink the others.
        "Broken Role": RuntimeError("boom"),
    })

    cands = asyncio.run(svc.search_candidates(
        skills=[],
        location="Denver, CO, United States",
        limit=3,
        titles=["Program Manager", "Project Manager", "Broken Role"],
    ))

    ids = [c["id"] for c in cands]
    if len(cands) != 3:
        print(f"FAIL [fanout count]: expected 3 merged candidates; got {ids!r}")
        failures += 1
    if len(set(ids)) != len(ids):
        print(f"FAIL [fanout dedupe]: duplicate ids {ids!r}")
        failures += 1
    if sum(1 for i in ids if "alice" in i) != 1:
        print(f"FAIL [fanout URL dedupe]: alice should appear exactly once; got {ids!r}")
        failures += 1
    # Round-robin: rank-0 results (alice from PM; her PjM dup dropped) come
    # before rank-1 results (bob, carol).
    if ids and "alice" not in ids[0]:
        print(f"FAIL [fanout interleave]: expected alice first; got {ids!r}")
        failures += 1
    if not failures:
        print("OK: fan-out merge cases passed")
    return failures


def run_schema_cases() -> int:
    """Pin the contact-field gating of the deep-research output schema: the
    descriptions are what activate Exa's contact-enrichment tool, and the
    flag controls per-hit billing."""
    failures = 0

    props_on = build_deep_research_output_schema(True)[
        "properties"]["candidates"]["items"]["properties"]
    for field in ("email", "phone"):
        if not str(props_on.get(field, {}).get("description") or "").strip():
            print(f"FAIL [schema-on]: {field} missing description; got {props_on.get(field)!r}")
            failures += 1

    props_off = build_deep_research_output_schema(False)[
        "properties"]["candidates"]["items"]["properties"]
    for field in ("email", "phone"):
        if field in props_off:
            print(f"FAIL [schema-off]: {field} present with flag off")
            failures += 1

    if not failures:
        print("OK: deep-research schema gating cases passed")
    return failures


if __name__ == "__main__":
    sys.exit(run())
