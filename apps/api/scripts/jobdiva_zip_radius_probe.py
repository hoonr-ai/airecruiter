"""A/B/C probe: does JobDiva TalentSearch honor zip-radius geo filters?

Settles the contradiction between the swagger (TalentSearchDef documents
`zipCode` + `withinMiles`) and the field note in
scripts/jobdiva_mainframe_search.py ("TalentSearch silently ignores
structured countries/states/zip/radius"; only the boolean dialect
`Within 30 miles of 75019` works).

Three identical searches, varying ONLY the geo mechanism:

    A (control)    skills=<boolean>, no geo at all
    B (structured) skills=<boolean>, zipCode=<zip>, withinMiles=<miles>
    C (dialect)    skills="<boolean> Within <miles> miles of <zip>"

Then compares result-set overlap and, using the offline zip index, the
share of returned candidates actually within the radius. Interpretation:

    B ids == A ids                  → structured zipCode/withinMiles IGNORED
    B ⊂ A and B in-radius% >> A     → structured geo HONORED
    (same logic for C vs A)

Outcome drives two flags in core/sourcing_config.py:
    JOBDIVA_ZIP_RADIUS_ENABLED          (structured fields; default ON)
    JOBDIVA_BOOLEAN_ZIP_DIALECT_ENABLED (boolean rewrite; default OFF)

Run (needs prod JobDiva creds in env):
    cd apps/api
    source .env
    venv/bin/python -m scripts.jobdiva_zip_radius_probe --dry-run
    venv/bin/python -m scripts.jobdiva_zip_radius_probe
    venv/bin/python -m scripts.jobdiva_zip_radius_probe --boolean '"Python"' --zip 85281 --miles 25
    venv/bin/python -m scripts.jobdiva_zip_radius_probe --states TX   # also probe structured states
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from core import JOBDIVA_API_URL  # noqa: E402
from services.jobdiva import jobdiva_service, get_field  # noqa: E402
from services import zip_index  # noqa: E402


def _cid(c: Dict[str, Any]) -> str:
    return str(get_field(c, ["candidateId", "CANDIDATEID", "id", "ID"]) or "")


def _cand_geo(c: Dict[str, Any]) -> Tuple[str, str, str]:
    city = str(get_field(c, ["city", "locationCity", "CITY"]) or "")
    state = str(get_field(c, ["state", "locationState", "STATE", "PROVINCE", "province"]) or "")
    zip5 = str(get_field(c, ["zipcode", "ZIPCODE", "zip", "ZIP", "postalCode", "POSTALCODE"]) or "")
    return city.strip(), state.strip(), zip5.strip()


def _distance_from_target(c: Dict[str, Any], target: Tuple[float, float]) -> Optional[float]:
    """Offline straight-line miles from the candidate to the target zip,
    via candidate zip centroid, else city+state centroid. None = unknown."""
    city, state, zip5 = _cand_geo(c)
    point = zip_index.zip_centroid(zip5) if zip5 else None
    if point is None and city and state:
        point = zip_index.city_state_centroid(city, state)
    if point is None:
        return None
    return zip_index._haversine_miles(point[0], point[1], target[0], target[1])


async def _talent_search_raw(
    token: str,
    skills_value: str,
    *,
    zip_code: str = "",
    within_miles: Optional[int] = None,
    states: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """POST /apiv2/jobdiva/TalentSearch with exact payload control —
    bypasses the service's flag logic so each arm sends precisely what
    the experiment calls for."""
    talent_def: Dict[str, Any] = {
        "skills": skills_value,
        "countries": "US",
        "states": states,
        "pageNumber": 0,
        "pageSize": limit,
    }
    if zip_code:
        talent_def["zipCode"] = zip_code
    if within_miles is not None:
        talent_def["withinMiles"] = int(within_miles)

    url = f"{JOBDIVA_API_URL}/apiv2/jobdiva/TalentSearch"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"talentSearchDef": talent_def}, headers=headers)
        if resp.status_code != 200:
            print(f"  !! HTTP {resp.status_code}: {resp.text[:300]}")
            return []
        data = resp.json()
    if isinstance(data, dict):
        return data.get("data") or data.get("candidates") or data.get("results") or []
    return data or []


def _analyze(
    label: str,
    rows: List[Dict[str, Any]],
    target: Tuple[float, float],
    miles: int,
) -> Dict[str, Any]:
    ids = {_cid(c) for c in rows if _cid(c)}
    with_zip = sum(1 for c in rows if _cand_geo(c)[2])
    with_city = sum(1 for c in rows if _cand_geo(c)[0])
    states = sorted({_cand_geo(c)[1].upper() for c in rows if _cand_geo(c)[1]})
    dists = [_distance_from_target(c, target) for c in rows]
    known = [d for d in dists if d is not None]
    within = sum(1 for d in known if d <= miles)
    summary = {
        "label": label,
        "count": len(rows),
        "ids": ids,
        "with_zip": with_zip,
        "with_city": with_city,
        "geo_known": len(known),
        "within_radius": within,
        "within_pct": round(100 * within / len(known), 1) if known else None,
        "median_miles": round(sorted(known)[len(known) // 2], 1) if known else None,
        "states": states,
    }
    print(
        f"  [{label}] count={summary['count']} geo_known={summary['geo_known']} "
        f"within_{miles}mi={summary['within_radius']} ({summary['within_pct']}%) "
        f"median={summary['median_miles']}mi states={','.join(states[:12])}"
    )
    return summary


def _compare(a: Dict[str, Any], b: Dict[str, Any], miles: int) -> None:
    same = a["ids"] == b["ids"]
    inter = len(a["ids"] & b["ids"])
    print(f"\n  {b['label']} vs {a['label']}: identical={same} overlap={inter}/{len(b['ids']) or 1}")
    if same:
        print(f"  → {b['label']}: geo mechanism IGNORED (identical result set)")
    else:
        a_pct = a["within_pct"] if a["within_pct"] is not None else -1
        b_pct = b["within_pct"] if b["within_pct"] is not None else -1
        if b_pct > a_pct + 10:
            print(
                f"  → {b['label']}: geo mechanism HONORED "
                f"(in-radius {b_pct}% vs control {a_pct}%)"
            )
        else:
            print(
                f"  → {b['label']}: result set differs but in-radius share is not "
                f"clearly better ({b_pct}% vs {a_pct}%) — inspect manually"
            )


async def run(args: argparse.Namespace) -> int:
    target = zip_index.zip_centroid(args.zip)
    if not target:
        print(f"error: --zip {args.zip} not in the offline zip index")
        return 2
    loc = zip_index.lookup_zip(args.zip)
    print(f"[probe] boolean : {args.boolean!r}")
    print(f"[probe] zip     : {args.zip} ({loc['city']}, {loc['state']})")
    print(f"[probe] radius  : {args.miles} mi | limit per arm: {args.limit}")
    print(f"[probe] API     : {JOBDIVA_API_URL}")

    if args.dry_run:
        from services.jobdiva_boolean_translator import rewrite_location_clauses_to_zip_dialect
        dialect = f"{args.boolean} Within {args.miles} miles of {args.zip}"
        wizard_boolean = (
            f'{args.boolean} AND "{loc["city"]}, {loc["state"]} {args.zip}" within {args.miles} mi'
        )
        print("\n[dry-run] arm A  skills:", repr(args.boolean))
        print("[dry-run] arm B  skills:", repr(args.boolean), f"+ zipCode={args.zip} withinMiles={args.miles}")
        print("[dry-run] arm C  skills:", repr(dialect))
        print("[dry-run] arm C2 skills:", repr(rewrite_location_clauses_to_zip_dialect(wizard_boolean)))
        if args.states:
            print(f"[dry-run] arm D  skills: {args.boolean!r} + states={args.states!r}")
        return 0

    token = await jobdiva_service.authenticate()
    if not token:
        print("error: JobDiva authentication failed (check env credentials)")
        return 2

    print("\n[probe] arm A — control (no geo) …")
    a_rows = await _talent_search_raw(token, args.boolean, limit=args.limit)
    a = _analyze("A:control", a_rows, target, args.miles)

    print("[probe] arm B — structured zipCode+withinMiles …")
    b_rows = await _talent_search_raw(
        token, args.boolean, zip_code=args.zip, within_miles=args.miles, limit=args.limit
    )
    b = _analyze("B:structured", b_rows, target, args.miles)

    dialect = f"{args.boolean} Within {args.miles} miles of {args.zip}"
    print("[probe] arm C — boolean dialect (trailing, space-joined) …")
    c_rows = await _talent_search_raw(token, dialect, limit=args.limit)
    c = _analyze("C:dialect", c_rows, target, args.miles)

    # Arm C2 — the shape production ACTUALLY emits when the dialect flag is
    # on: the translator rewrite of a wizard-style boolean, which yields an
    # AND-joined clause ('... AND Within N miles of ZIP'). If JobDiva only
    # parses the trailing space-joined form, C passes but C2 degrades into
    # keyword poisoning — so C2, not C, gates the flag.
    from services.jobdiva_boolean_translator import rewrite_location_clauses_to_zip_dialect
    wizard_boolean = (
        f'{args.boolean} AND "{loc["city"]}, {loc["state"]} {args.zip}" within {args.miles} mi'
    )
    c2_skills = rewrite_location_clauses_to_zip_dialect(wizard_boolean)
    print(f"[probe] arm C2 — translator output shape: {c2_skills!r} …")
    c2_rows = await _talent_search_raw(token, c2_skills, limit=args.limit)
    c2 = _analyze("C2:dialect-AND", c2_rows, target, args.miles)

    d = None
    if args.states:
        print(f"[probe] arm D — structured states={args.states!r} …")
        d_rows = await _talent_search_raw(token, args.boolean, states=args.states, limit=args.limit)
        d = _analyze("D:states", d_rows, target, args.miles)

    print("\n[probe] ===== VERDICTS =====")
    _compare(a, b, args.miles)
    _compare(a, c, args.miles)
    _compare(a, c2, args.miles)
    if d is not None:
        _compare(a, d, args.miles)

    print(
        "\n[probe] flag guidance:\n"
        "  B honored  → keep JOBDIVA_ZIP_RADIUS_ENABLED=true (default)\n"
        "  B ignored  → no harm; consider JOBDIVA_BOOLEAN_ZIP_DIALECT_ENABLED=true IF C2 is honored\n"
        "  C2 ignored → leave the dialect flag off even if C passes — production emits the\n"
        "               AND-joined shape, and unparsed 'within/miles' words poison the search"
    )

    if args.out:
        payload = {
            k: {**v, "ids": sorted(v["ids"])}
            for k, v in {"A": a, "B": b, "C": c, "C2": c2, **({"D": d} if d else {})}.items()
        }
        Path(args.out).write_text(json.dumps(payload, indent=2))
        print(f"[probe] wrote {args.out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--boolean", default='"Java"', help="keyword boolean used by every arm")
    p.add_argument("--zip", default="75019", help="target zip (default 75019 — Coppell, TX)")
    p.add_argument("--miles", type=int, default=30)
    p.add_argument("--limit", type=int, default=100, help="pageSize per arm")
    p.add_argument("--states", default="", help='also run arm D with structured states (e.g. "TX")')
    p.add_argument("--out", default="", help="write per-arm JSON results to this path")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
