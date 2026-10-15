"""Golden-fixture parity: services/launch_sessions/rules.py vs apps/web/lib/launch-rules.ts.

Every case in tests/fixtures/launch_rules/*.json was produced by running the
TypeScript rules (the browser's Step-5 Source & Launch). The server launch
runner uses the Python port, so each case must come out identical here —
including the JS traps (truthy ``[]``, ``bool`` is not a number, String()
of numbers, JS whitespace / ASCII-only regex classes, UTF-16 lengths, stable
sort ties). The same fixtures run in node --test (lib/launch-rules.test.ts).

``_source.json`` holds the sha256 of the launch-rules.ts the fixtures were
generated from; ``test_fixtures_match_launch_rules_ts`` fails when the TS
changed without a regeneration, so this suite (CI's test-api, which gates
deploys) also guards the TS side.
"""
import copy
import hashlib
import json
import math
import re
import time
from pathlib import Path

import pytest

from services.launch_sessions import rules

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "launch_rules"
WEB_LIB = Path(__file__).resolve().parents[2] / "web" / "lib"


def _run_stream(steps, keys_cache=None):
    state = rules.initial_stream_state()
    accumulated = []
    started = False
    for step in steps:
        if "begin" in step:
            if started:
                accumulated = accumulated + state["runList"]
            state = rules.begin_stream_attempt(state, step["begin"])
            started = True
        else:
            state = rules.apply_stream_event(state, step["event"], keys_cache=keys_cache)
    accumulated = accumulated + state["runList"]
    return state, accumulated


def _stream_then_select(i):
    state, accumulated = _run_stream(i["steps"])
    opts = dict(
        launched_keys=set(i.get("launchedKeys") or []),
        launched_ids=set(i.get("launchedIds") or []),
        limit=i.get("limit"),
    )
    pool = rules.choose_selection_pool(state["candidates"], accumulated)
    return {
        "table": [rules.candidate_row_id(c) for c in state["candidates"]],
        "selection": rules.compute_auto_launch_selection(state["candidates"], **opts),
        "exclusions": [rules.get_candidate_exclusion_reason(c) for c in state["candidates"]],
        "pool": [rules.candidate_row_id(c) for c in pool],
        "poolSelection": rules.compute_auto_launch_selection(pool, **opts),
        "qualified": rules.count_qualified(accumulated),
    }


def _selection(i):
    return rules.compute_auto_launch_selection(
        i["pool"], launched_keys=_s(i, "launchedKeys"), launched_ids=_s(i, "launchedIds"), limit=i.get("limit"),
        min_score=i["minScore"] if "minScore" in i else rules.LAUNCH_MIN_SCORE,
        blocked=i.get("blocked"), with_reasons=bool(i.get("withReasons")),
    )


def _apply_stream_events(i):
    state, accumulated = _run_stream(i["steps"])
    return {"state": state, "qualified": rules.count_qualified(accumulated)}


def _launched_sets(i):
    out = rules.launched_sets_from_response(i.get("items"))
    return {"keys": sorted(out["keys"]), "ids": sorted(out["ids"])}


def _s(i, key):
    return set(i.get(key) or [])


ADAPTERS = {
    "isValidLaunchEmail": lambda i: rules.is_valid_launch_email(i.get("value")),
    "launchPhoneDigits": lambda i: rules.launch_phone_digits(i.get("value")),
    "isValidLaunchPhone": lambda i: rules.is_valid_launch_phone(i.get("value")),
    "normalizeDncPhone": lambda i: rules.normalize_dnc_phone(i.get("value")),
    "betterPhoneUI": lambda i: rules.better_phone_ui(i.get("current"), i.get("incoming")),
    "getCandidateLaunchEmail": lambda i: rules.get_candidate_launch_email(i["candidate"]),
    "getCandidateLaunchPhone": lambda i: rules.get_candidate_launch_phone(i["candidate"]),
    "isLaunchableNow": lambda i: rules.is_launchable_now(i["candidate"]),
    "getCandidateDisplayName": lambda i: rules.get_candidate_display_name(i["candidate"]),
    "isPlaceholderEmailUI": lambda i: rules.is_placeholder_email_ui(i.get("value")),
    "candIsJobDivaUI": lambda i: rules.cand_is_jobdiva_ui(i["candidate"]),
    "deduplicateCandidatesUI": lambda i: rules.deduplicate_candidates_ui(i["candidates"]),
    "normalizeCompanyName": lambda i: rules.normalize_company_name(i.get("value")),
    "extractCompanyFromHeadline": lambda i: rules.extract_company_from_headline(i.get("headline")),
    "getCandidateCurrentCompanies": lambda i: rules.get_candidate_current_companies(i["candidate"]),
    "isEmployedByClient": lambda i: rules.is_employed_by_client(i["candidate"], i.get("job")),
    "getCandidateExclusionReason": lambda i: rules.get_candidate_exclusion_reason(i["candidate"]),
    "withClientConflictFlag": lambda i: rules.with_client_conflict_flag(i["candidate"], i.get("job")),
    "isLaunchEligibleScore": lambda i: rules.is_launch_eligible_score(i.get("score")),
    "clampLaunchCount": lambda i: rules.clamp_launch_count(i.get("n")),
    "countQualified": lambda i: rules.count_qualified(i["candidates"]),
    "candidateRowId": lambda i: rules.candidate_row_id(i["candidate"]),
    "chooseSelectionPool": lambda i: [
        rules.candidate_row_id(c) for c in rules.choose_selection_pool(i["table"], i["accumulated"])
    ],
    "computeAutoLaunchSelection": _selection,
    "launchedSetsFromResponse": _launched_sets,
    "computeHardFilterSkips": lambda i: rules.compute_hard_filter_skips(i["candidates"], _s(i, "selectedIds")),
    "selectCandidatesMissingContact": lambda i: rules.select_candidates_missing_contact(
        i["candidates"], _s(i, "selectedIds"), _s(i, "hardFilterSkipIds"),
    ),
    "extractLinkedInFromText": lambda i: rules.extract_linkedin_from_text(i.get("text")),
    "looksLikeLinkedInProfile": lambda i: rules.looks_like_linkedin_profile(i.get("url")),
    "resolveLinkedInUrl": lambda i: rules.resolve_linkedin_url(i["candidate"]),
    "getCandidateSeedEmail": lambda i: rules.get_candidate_seed_email(i["candidate"]),
    "getCandidateLookupName": lambda i: rules.get_candidate_lookup_name(i["candidate"]),
    "buildEnrichRequest": lambda i: rules.build_enrich_request(
        i["candidate"], i["candidateId"], i["linkedinUrl"], i.get("jobdivaId"),
    ),
    "interpretEnrichAnswer": lambda i: rules.interpret_enrich_answer(
        i.get("answer"), i["launchableNow"], i.get("heldEmail") or "",
    ),
    "dncDroppedIds": lambda i: rules.dnc_dropped_ids(
        i["candidates"], _s(i, "selectedIds"), i.get("overrides") or {}, _s(i, "dncPhones"),
    ),
    "splitReadyNeedsContact": lambda i: rules.split_ready_needs_contact(
        i["candidates"], selected_ids=_s(i, "selectedIds"), skip_ids=_s(i, "skipIds"),
        overrides=i.get("overrides") or {}, jobdiva_id=i.get("jobdivaId"),
    ),
    "mergeContactOverrides": lambda i: rules.merge_contact_overrides(i["pending"], i["provided"]),
    "applyContactOverrides": lambda i: rules.apply_contact_overrides(i["candidates"], i.get("overrides")),
    "selectSaveCandidates": lambda i: rules.select_save_candidates(
        i["candidates"], launch_ids=_s(i, "launchIds"), job=i.get("job"), dnc_phones=_s(i, "dncPhones"),
    ),
    "buildSaveCandidatePayload": lambda i: rules.build_save_candidate_payload(i["candidate"]),
    "buildLaunchSavePayload": lambda i: rules.build_launch_save_payload(
        i["candidates"], launch_ids=_s(i, "launchIds"), overrides=i.get("overrides"),
        job=i.get("job"), dnc_phones=_s(i, "dncPhones"),
    ),
    "mapStageToStatus": lambda i: rules.map_stage_to_status(i["stage"]),
    "mergeSourceStatus": lambda i: rules.merge_source_status(i["prev"], i["status"]),
    "applyStreamPatch": lambda i: rules.apply_stream_patch(i["row"], i["patch"]),
    "applyStreamEvents": _apply_stream_events,
    "streamThenSelect": _stream_then_select,
}


def _strict_equal(a, b):
    """JSON equality that does NOT let True == 1 or 1 == "1" slip through."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _load_cases():
    cases = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        if path.name.startswith("_"):  # metadata (_source.json), not a fixture
            continue
        fixture = json.loads(path.read_text())
        for case in fixture["cases"]:
            cases.append(pytest.param(fixture["function"], case, id=f"{path.stem}::{case['name']}"))
    return cases


CASES = _load_cases()


def test_fixtures_present_and_known():
    assert len(CASES) > 300
    functions = {p.values[0] for p in CASES}
    assert functions <= set(ADAPTERS), functions - set(ADAPTERS)
    # Every rule the server runner relies on is covered by at least one case.
    assert set(ADAPTERS) <= functions, set(ADAPTERS) - functions


@pytest.mark.parametrize("function,case", CASES)
def test_python_port_reproduces_ts(function, case):
    snapshot = copy.deepcopy(case["input"])
    actual = json.loads(json.dumps(ADAPTERS[function](case["input"]), allow_nan=False))
    assert _strict_equal(actual, case["expected"]), (
        f"{function} :: {case['name']}\n  expected={json.dumps(case['expected'])[:2000]}"
        f"\n  actual  ={json.dumps(actual)[:2000]}"
    )
    assert case["input"] == snapshot, "rule mutated its input"


# ── Python-only traps the fixtures cannot express (no NaN / tuples in JSON) ──

def test_bool_is_not_a_score_or_status():
    assert rules.is_launch_eligible_score(True) is False
    assert rules.is_launch_eligible_score(60) is True
    assert rules.get_candidate_exclusion_reason({"status": True}) == ""
    sel = rules.compute_auto_launch_selection(
        [{"candidate_id": "a", "match_score": True}], launched_keys=set(), launched_ids=set(), limit=5,
    )
    assert sel == {"ids": [], "eligibleTotal": 0, "belowFloor": 0, "unscored": 1}


def test_non_finite_scores():
    assert rules.is_launch_eligible_score(math.nan) is False
    assert rules.is_launch_eligible_score(math.inf) is False
    assert rules.clamp_launch_count(math.nan) == 0  # TS NaN slices to nothing
    assert rules.clamp_launch_count(math.inf) == rules.AUTO_LAUNCH_MAX_COUNT
    payload = rules.build_save_candidate_payload({"candidate_id": "x", "match_score": math.nan})
    assert payload["match_score"] is None  # JSON.stringify(NaN) === "null"


def test_js_string_and_number_coercion():
    assert rules._js_str(85.0) == "85"
    assert rules._js_str(0.000015) == "0.000015"
    assert rules._js_str(1e-7) == "1e-7"
    assert rules._js_str(1e21) == "1e+21"
    assert rules._js_str([1, None, "a"]) == "1,,a"
    assert rules._js_to_number(" 75 ") == 75
    assert rules._js_to_number("0x1A") == 26
    assert math.isnan(rules._js_to_number("1_000"))
    assert math.isnan(rules._js_to_number("inf"))
    assert rules._js_to_number("") == 0


def test_reducer_does_not_mutate_state():
    state = rules.initial_stream_state()
    state = rules.apply_stream_event(state, {"type": "candidate", "data": {"candidate_id": "a", "phone": "408-555-1234"}})
    frozen = copy.deepcopy(state)
    rules.apply_stream_event(state, {"type": "candidate_detail", "candidate_id": "a", "patch": {"phone": "", "match_score": 70}})
    rules.apply_stream_event(state, {"type": "candidate_detail", "candidate_id": "a", "stage": "dropped", "patch": {}})
    assert state == frozen


@pytest.mark.parametrize(
    "case", [p.values[1] for p in CASES if p.values[0] == "applyStreamEvents"],
    ids=lambda c: c["name"],
)
def test_keys_cache_does_not_change_the_reducer(case):
    """The runner passes one keys_cache per run; results must equal the TS."""
    cache = {}
    state, accumulated = _run_stream(case["input"]["steps"], keys_cache=cache)
    actual = json.loads(json.dumps({"state": state, "qualified": rules.count_qualified(accumulated)}))
    assert _strict_equal(actual, case["expected"])


def test_dedup_scales_for_a_full_search_stream():
    """A 4-attempt full search streams ~1-2k rows plus patches; the reducer
    re-runs the FE de-dup after every event, so through StreamRun (which owns
    the key cache) it must stay O(rows) per event (the TS O(rows²) scan,
    ported naively, would not)."""
    run = rules.StreamRun()
    run.begin_attempt()
    events = []
    for i in range(600):
        events.append({"type": "candidate", "data": {
            "candidate_id": f"c{i}", "source": "LinkedIn" if i % 3 else "JobDiva-Applicants",
            "email": f"p{i % 450}@x.com", "phone": f"408-555-{i % 500:04d}", "firstName": "A", "lastName": f"B{i}",
        }})
        events.append({"type": "candidate_detail", "candidate_id": f"c{i}", "stage": "scored", "patch": {"match_score": i % 100}})
    started = time.perf_counter()
    run.apply_many(events)
    elapsed = time.perf_counter() - started
    assert len(run.table) <= 600
    assert elapsed < 10, elapsed


# ── The TS source the fixtures came from, and the copies it inlines ─────────

def _ts_source(name):
    path = WEB_LIB / name
    if not path.exists():  # an API-only checkout (e.g. a Docker build context)
        pytest.skip(f"apps/web/lib/{name} not in this checkout")
    return path


def test_fixtures_match_launch_rules_ts():
    """Fails when launch-rules.ts changed without regenerating the fixtures —
    the regeneration is what re-checks the TS (and then this port) against
    every case."""
    source = _ts_source("launch-rules.ts")
    stamp = json.loads((FIXTURE_DIR / "_source.json").read_text())
    actual = hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert stamp["sha256"] == actual, (
        "apps/web/lib/launch-rules.ts changed since the launch-rules fixtures were generated. "
        f"Regenerate them ({stamp['regenerate']}), then port any rule change to "
        "services/launch_sessions/rules.py until this suite passes."
    )


def test_floor_matches_web_outreach_floor():
    text = _ts_source("match-score.ts").read_text()
    consts = dict(re.findall(r"export const (\w+) = (\w+);", text))
    floor = consts["OUTREACH_MIN_SCORE"]
    floor = consts.get(floor, floor)
    assert rules.LAUNCH_MIN_SCORE == int(floor)
    assert rules.QUALIFIED_SCORE_THRESHOLD == rules.LAUNCH_MIN_SCORE


def test_dnc_normalisation_matches_backend_dnc_matching():
    from utils.phone import normalize_phone

    for v in [None, "", "408-555-1234", "1 (408) 555-1234", "+44 20 7946 0958", "5551234", "abc", "24085551234"]:
        assert rules.normalize_dnc_phone(v) == normalize_phone(v), v


# ── StreamRun: the server's entry point must reproduce the fixtures ─────────

def _replace_then_appends(steps):
    begins = [s["begin"] for s in steps if "begin" in s]
    return "begin" in steps[0] and begins[0] == "replace" and all(b == "append" for b in begins[1:])


def _run_with_stream_run(steps):
    run = rules.StreamRun()
    batch = []
    for step in steps:
        if "begin" in step:
            run.apply_many(batch)
            batch = []
            run.begin_attempt()
        else:
            batch.append(step["event"])
    run.apply_many(batch)
    return run


@pytest.mark.parametrize(
    "case", [p.values[1] for p in CASES if p.values[0] == "applyStreamEvents"
             and _replace_then_appends(p.values[1]["input"]["steps"])],
    ids=lambda c: c["name"],
)
def test_stream_run_reproduces_the_reducer(case):
    run = _run_with_stream_run(case["input"]["steps"])
    actual = json.loads(json.dumps({"state": run.state, "qualified": run.qualified()}))
    assert _strict_equal(actual, case["expected"])


@pytest.mark.parametrize(
    "case", [p.values[1] for p in CASES if p.values[0] == "streamThenSelect"],
    ids=lambda c: c["name"],
)
def test_stream_run_selection_pool(case):
    i = case["input"]
    run = _run_with_stream_run(i["steps"])
    selection = rules.compute_auto_launch_selection(
        run.selection_pool(), launched_keys=_s(i, "launchedKeys"), launched_ids=_s(i, "launchedIds"),
        limit=i.get("limit"),
    )
    exp = case["expected"]
    assert [rules.candidate_row_id(c) for c in run.table] == exp["table"]
    assert [rules.candidate_row_id(c) for c in run.selection_pool()] == exp["pool"]
    assert _strict_equal(json.loads(json.dumps(selection)), exp["poolSelection"])
    assert run.qualified() == exp["qualified"]


# ── absorbed: who was folded into whom (Python-only bookkeeping) ────────────

@pytest.mark.parametrize(
    "case", [p.values[1] for p in CASES if p.values[0] == "deduplicateCandidatesUI"],
    ids=lambda c: c["name"],
)
def test_absorbed_accounts_for_every_merged_row(case):
    rows = case["input"]["candidates"]
    absorbed = {}
    out = rules.deduplicate_candidates_ui(rows, absorbed=absorbed)
    assert _strict_equal(json.loads(json.dumps(out)), case["expected"])  # same output with the out-param
    resolved = rules.resolve_absorbed(absorbed, out)
    out_ids = {rules.candidate_row_id(c) for c in out}
    for row in rows:
        rid = rules.candidate_row_id(row)
        if rid:
            assert rid in out_ids or resolved.get(rid) in out_ids, rid
    assert not set(resolved) & out_ids
    assert set(resolved.values()) <= out_ids


def test_absorbed_follows_chains_and_forgets_rows_that_return():
    run = rules.StreamRun()
    run.begin_attempt()
    run.apply_many([
        {"type": "candidate", "data": {"candidate_id": "li", "source": "LinkedIn", "email": "p@x.com"}},
        {"type": "candidate", "data": {"candidate_id": "exa", "source": "Exa", "email": "p@x.com", "phone": "408-555-1234"}},
        {"type": "candidate", "data": {"candidate_id": "jd", "source": "JobDiva-Applicants", "email": "p@x.com"}},
    ])
    assert [c["candidate_id"] for c in run.table] == ["jd"]
    assert run.absorbed() == {"li": "jd", "exa": "jd"}  # li → exa → jd resolved to the final survivor
    run.apply({"type": "candidate_detail", "candidate_id": "li", "stage": "dropped", "patch": {}})
    run.apply({"type": "candidate", "data": {"candidate_id": "li", "source": "LinkedIn", "email": "other@x.com"}})
    assert sorted(c["candidate_id"] for c in run.table) == ["jd", "li"]
    assert run.absorbed() == {"exa": "jd"}


def test_stream_run_accumulates_attempts_like_handle_run_search():
    run = rules.StreamRun()
    run.begin_attempt()
    run.apply({"type": "candidate", "data": {"candidate_id": "a", "source": "Exa", "email": "a@x.com", "match_score": 40}})
    run.begin_attempt()
    run.apply_many([
        {"type": "candidate", "data": {"candidate_id": "a", "match_score": 99}},  # seen: skipped entirely
        {"type": "candidate_detail", "candidate_id": "a", "stage": "scored", "patch": {"match_score": 61}},
        {"type": "candidate", "data": {"candidate_id": "b", "source": "Exa", "match_score": "75"}},
    ])
    assert run.attempts == 2
    assert [c["candidate_id"] for c in run.accumulated] == ["a", "b"]
    assert run.accumulated[0]["match_score"] == 40  # attempt 1's copy keeps attempt 1's score
    assert next(c for c in run.table if c["candidate_id"] == "a")["match_score"] == 61
    assert run.qualified() == 1  # the string "75" coerces; a's accumulated 40 does not count
    assert run.selection_pool() is run.table


def test_selection_reasons_and_no_floor_on_python_values():
    pool = [
        {"candidate_id": "a", "match_score": 80},
        {"candidate_id": "b", "match_score": True},  # a bool is not a score
        {"candidate_id": "c", "match_score": 61},
        {"candidate_id": "d"},
    ]
    sel = rules.compute_auto_launch_selection(
        pool, launched_keys=set(), launched_ids=set(), limit=1, blocked={"c": "in_flight"}, with_reasons=True,
    )
    assert sel["ids"] == ["a"]
    assert sel["skipped"] == [
        {"id": "b", "reason": "unscored"}, {"id": "c", "reason": "in_flight"}, {"id": "d", "reason": "unscored"},
    ]
    quick = rules.compute_auto_launch_selection(
        pool, launched_keys=set(), launched_ids=set(), limit=10, min_score=None,
    )
    assert quick["ids"] == ["a", "c", "b", "d"]


def test_a_merged_jobdiva_row_never_seeds_another_sources_email():
    """Review of PR 4: a JobDiva row with an Auto_* (or no) email takes a
    LinkedIn row's extracted email on a merge; Launch must still look the
    email up (no seed), and a found one replaces it, as before seeds."""
    li = "https://www.linkedin.com/in/jane-doe"
    [jd] = rules.deduplicate_candidates_ui([
        {"candidate_id": "jd", "source": "JobDiva-TalentSearch", "email": "Auto_77@jobdiva.com", "profile_url": li},
        {"candidate_id": "li", "source": "LinkedIn-Exa", "email": "jane.extracted@acme.com", "profile_url": li},
    ])
    assert jd["candidate_id"] == "jd" and jd["email"] == "jane.extracted@acme.com"
    assert rules.get_candidate_seed_email(jd) == ""
    assert "email" not in rules.build_enrich_request(jd, "jd", li, "26-1")
    found = {"status": "success", "email": "jane@found.test", "phone": "+15125550100"}
    assert rules.interpret_enrich_answer(found, True, rules.get_candidate_seed_email(jd))["override"] == {
        "phone": "+15125550100", "email": "jane@found.test",
    }
    # The JobDiva row's own email is still the seed.
    [own] = rules.deduplicate_candidates_ui([
        {"candidate_id": "jd", "source": "JobDiva-TalentSearch", "email": "jane@acme.com", "profile_url": li},
        {"candidate_id": "li", "source": "LinkedIn-Exa", "email": "other@acme.com", "profile_url": li},
    ])
    assert rules.get_candidate_seed_email(own) == "jane@acme.com"
