"""Payload-variant probe for JobDiva TalentSearch location/skill precision.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTCOME (live run 2026-07-19, prod creds) — the root cause was found and
fixed, so the arms below are of historical interest only:

  Every arm in this file returned the IDENTICAL result set — including a
  gibberish keyword and a self-contradicting boolean — because the payload
  wrapper `{"talentSearchDef": {...}}` (and its string-typed fields) is
  silently discarded by the server, which then answers an EMPTY search
  with its default unfiltered dump (~2.5k rows).

  Per the swagger (https://api.jobdiva.com/swagger?group=Version 2), the
  v2 TalentSearch body carries the TalentSearchDef fields at the TOP
  LEVEL: skills is an ARRAY of plain AND'd terms; states/countries are
  ARRAYS of 2-letter codes; zipCode+withinMiles are honored (98.8%
  in-radius vs 8.1% unfiltered); titleSearch works stand-alone;
  advancedSkills/location always return 0 rows; pageNumber/pageSize are
  ignored (the full filtered set returns in one response); boolean syntax
  inside a term kills the request.

  Production now sends that shape — see services/jobdiva.py
  `_search_talent_pool` / `_fetch_talent_search_rows`.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Follow-up to scripts/jobdiva_zip_radius_probe.py, which established live
(2026-07-19, prod creds) that the WRAPPED TalentSearch payload ignores
every geo mechanism it documents: structured zipCode+withinMiles (arm B),
the trailing boolean dialect (arm C) and the AND-joined translator shape
(arm C2) all returned result sets identical to the no-geo control. This
probe answered the next question: WHICH payload variations actually narrow
the result set (answer: none of the boolean-side ones — the body shape was
the bug).

Arms (same base boolean everywhere, vary ONE thing):

    A   control                 skills=<boolean>
    D   structured states       skills=<boolean>, states=<ST>
    E   wizard-chip verbatim    skills=<boolean> AND "City, ST ZIP" within N mi
                                (what a frontend location chip looks like if it
                                ever reaches JobDiva untranslated — poisoning test)
    F   zip keyword             skills=<boolean> AND "ZIP"
    G   nearby-cities OR        skills=<boolean> AND ("City1" OR "City2" ...)
                                cities within the radius, from the offline zip index
    H   state keyword           skills=<boolean> AND "ST"
    I   city+state phrases      skills=<boolean> AND ("City1, ST" OR "City2, ST" ...)
    J   structured titles       skills=<boolean>, titles=<title> (undocumented field)

Each arm reports: result count, offline in-radius share (zip index), state
spread, and a title-relevance share (how many returned candidates' titles
contain a base-boolean token — a rough skill-precision proxy).

Run (needs prod JobDiva creds in apps/api/.env):
    cd apps/api
    venv/bin/python -m scripts.jobdiva_payload_variants_probe --dry-run
    venv/bin/python -m scripts.jobdiva_payload_variants_probe
    venv/bin/python -m scripts.jobdiva_payload_variants_probe \
        --boolean '"Python"' --zip 85281 --miles 25 --title "Python Developer"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
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


def _cand_title(c: Dict[str, Any]) -> str:
    return str(get_field(c, ["title", "candidateTitle", "TITLE", "currentTitle"]) or "")


def _distance_from_target(c: Dict[str, Any], target: Tuple[float, float]) -> Optional[float]:
    city, state, zip5 = _cand_geo(c)
    point = zip_index.zip_centroid(zip5) if zip5 else None
    if point is None and city and state:
        point = zip_index.city_state_centroid(city, state)
    if point is None:
        return None
    return zip_index._haversine_miles(point[0], point[1], target[0], target[1])


def cities_within_radius(
    zip5: str, miles: float, max_cities: int = 12
) -> List[Tuple[str, str]]:
    """(city, state) pairs whose zip centroids fall inside the radius,
    largest first (zip count as a population proxy), anchor city first."""
    target = zip_index.zip_centroid(zip5)
    if not target:
        return []
    counts: Dict[Tuple[str, str], int] = {}
    for z, rec in zip_index._load().items():  # rec = [city, state, lat, lng]
        d = zip_index._haversine_miles(rec[2], rec[3], target[0], target[1])
        if d <= miles:
            key = (rec[0], rec[1])
            counts[key] = counts.get(key, 0) + 1
    anchor = zip_index.zip_city_state(zip5)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    out: List[Tuple[str, str]] = []
    if anchor and anchor in counts:
        out.append(anchor)
    for key, _n in ranked:
        if key not in out:
            out.append(key)
        if len(out) >= max_cities:
            break
    return out


async def _talent_search_raw(
    token: str,
    skills_value: str,
    *,
    states: str = "",
    titles: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    talent_def: Dict[str, Any] = {
        "skills": skills_value,
        "countries": "US",
        "states": states,
        "pageNumber": 0,
        "pageSize": limit,
    }
    if titles:
        talent_def["titles"] = titles
    url = f"{JOBDIVA_API_URL}/apiv2/jobdiva/TalentSearch"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data: Any = None
    for attempt in range(4):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, json={"talentSearchDef": talent_def}, headers=headers)
            if resp.status_code != 200:
                print(f"  !! HTTP {resp.status_code}: {resp.text[:300]}")
                return []
            data = resp.json()
            break
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout) as exc:
            # JobDiva intermittently truncates large chunked responses.
            print(f"  .. transport error (attempt {attempt + 1}/4): {exc}")
            await asyncio.sleep(2.0 * (attempt + 1))
    else:
        return []
    if isinstance(data, dict):
        return data.get("data") or data.get("candidates") or data.get("results") or []
    return data or []


def _base_tokens(boolean: str) -> List[str]:
    return [t.lower() for t in re.findall(r'"([^"]+)"', boolean) or boolean.split() if t.strip()]


def _analyze(
    label: str,
    rows: List[Dict[str, Any]],
    target: Tuple[float, float],
    miles: int,
    state: str,
    tokens: List[str],
) -> Dict[str, Any]:
    ids = {_cid(c) for c in rows if _cid(c)}
    dists = [_distance_from_target(c, target) for c in rows]
    known = [d for d in dists if d is not None]
    within = sum(1 for d in known if d <= miles)
    in_state = sum(1 for c in rows if _cand_geo(c)[1].upper() == state.upper())
    title_hits = sum(
        1 for c in rows if any(tok in _cand_title(c).lower() for tok in tokens)
    )
    summary = {
        "label": label,
        "count": len(rows),
        "ids": ids,
        "geo_known": len(known),
        "within_radius": within,
        "within_pct": round(100 * within / len(known), 1) if known else None,
        "in_state": in_state,
        "in_state_pct": round(100 * in_state / len(rows), 1) if rows else None,
        "median_miles": round(sorted(known)[len(known) // 2], 1) if known else None,
        "title_hit_pct": round(100 * title_hits / len(rows), 1) if rows else None,
    }
    print(
        f"  [{label}] count={summary['count']} "
        f"within_{miles}mi={summary['within_radius']}/{summary['geo_known']} ({summary['within_pct']}%) "
        f"in_{state}={summary['in_state']} ({summary['in_state_pct']}%) "
        f"median={summary['median_miles']}mi title_hit={summary['title_hit_pct']}%"
    )
    return summary


async def run(args: argparse.Namespace) -> int:
    target = zip_index.zip_centroid(args.zip)
    if not target:
        print(f"error: --zip {args.zip} not in the offline zip index")
        return 2
    loc = zip_index.lookup_zip(args.zip)
    state = loc["state"]
    tokens = _base_tokens(args.boolean)

    nearby = cities_within_radius(args.zip, args.miles, max_cities=args.max_cities)
    or_cities = " OR ".join(f'"{c}"' for c, _s in nearby)
    or_city_states = " OR ".join(f'"{c}, {s}"' for c, s in nearby)

    arms: List[Tuple[str, Dict[str, Any]]] = [
        ("A:control", {"skills": args.boolean}),
        ("D:structured-states", {"skills": args.boolean, "states": state}),
        (
            "E:wizard-chip-verbatim",
            {"skills": f'{args.boolean} AND "{loc["city"]}, {state} {args.zip}" within {args.miles} mi'},
        ),
        ("F:zip-keyword", {"skills": f'{args.boolean} AND "{args.zip}"'}),
        ("G:nearby-cities-OR", {"skills": f"{args.boolean} AND ({or_cities})"}),
        ("H:state-keyword", {"skills": f'{args.boolean} AND "{state}"'}),
        ("I:city-state-phrases", {"skills": f"{args.boolean} AND ({or_city_states})"}),
        ("J:structured-titles", {"skills": args.boolean, "titles": args.title}),
    ]

    print(f"[probe] boolean : {args.boolean!r}")
    print(f"[probe] zip     : {args.zip} ({loc['city']}, {state}) radius {args.miles} mi")
    print(f"[probe] nearby  : {nearby}")
    print(f"[probe] API     : {JOBDIVA_API_URL}\n")

    if args.dry_run:
        for label, kw in arms:
            print(f"[dry-run] {label}: {kw}")
        return 0

    token = await jobdiva_service.authenticate()
    if not token:
        print("error: JobDiva authentication failed (check env credentials)")
        return 2

    results: Dict[str, Dict[str, Any]] = {}
    for label, kw in arms:
        print(f"[probe] {label} …")
        rows = await _talent_search_raw(token, kw["skills"], states=kw.get("states", ""),
                                        titles=kw.get("titles", ""), limit=args.limit)
        results[label] = _analyze(label, rows, target, args.miles, state, tokens)
        await asyncio.sleep(1.0)

    a = results["A:control"]
    print("\n[probe] ===== vs control =====")
    for label, r in results.items():
        if label == "A:control":
            continue
        same = r["ids"] == a["ids"]
        sub = r["ids"] <= a["ids"] and not same
        print(
            f"  {label}: identical={same} subset={sub} "
            f"count {a['count']}→{r['count']} within% {a['within_pct']}→{r['within_pct']} "
            f"in_state% {a['in_state_pct']}→{r['in_state_pct']}"
        )

    if args.out:
        payload = {k: {**v, "ids": sorted(v["ids"])} for k, v in results.items()}
        Path(args.out).write_text(json.dumps(payload, indent=2))
        print(f"[probe] wrote {args.out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--boolean", default='"Java"')
    p.add_argument("--zip", default="75019")
    p.add_argument("--miles", type=int, default=30)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--max-cities", type=int, default=12)
    p.add_argument("--title", default="Java Developer", help="value for the structured-titles arm")
    p.add_argument("--out", default="")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
