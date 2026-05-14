"""Follow-up: resolve target emails to JobDiva candidate IDs and cross-check
against the candidate IDs returned by the four probes.

Reads the existing dumps in --out and writes `email_lookup.json` +
appends a "Probe E" section to report.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))


async def _main(args: argparse.Namespace) -> int:
    from services.jobdiva import JobDivaService

    out_dir = Path(args.out).expanduser().resolve()
    targets = [e.strip().lower() for e in args.target_emails.split(",") if e.strip()]

    jd = JobDivaService()
    token = await jd.authenticate()
    if not token:
        print("✗ JobDiva auth failed", file=sys.stderr)
        return 2

    # 1. Resolve each email → candidate_id (+ try full candidate detail)
    resolved: Dict[str, Dict[str, Any]] = {}
    for email in targets:
        cid = await jd.search_candidate_profile(email=email)
        info: Dict[str, Any] = {"email": email, "candidate_id": str(cid) if cid else None}
        if cid:
            # Pull CandidatesDetail to confirm location, etc.
            try:
                detail_map = await jd._fetch_candidate_details_batch(token, [str(cid)])
                detail = detail_map.get(str(cid)) or {}
                info["detail_keys"] = list(detail.keys())[:30]
                info["detail_email"] = detail.get("email") or detail.get("EMAIL")
                info["detail_state"] = detail.get("state") or detail.get("STATE")
                info["detail_city"] = detail.get("city") or detail.get("CITY")
                info["detail_country"] = detail.get("country") or detail.get("COUNTRY")
                info["detail_title"] = detail.get("title") or detail.get("TITLE")
                info["detail_last_modified"] = (
                    detail.get("lastModified") or detail.get("LASTMODIFIED")
                    or detail.get("lastUpdated") or detail.get("LASTUPDATED")
                )
            except Exception as e:
                info["detail_error"] = str(e)
        resolved[email] = info
        print(f"  {email} → {info}")

    # 2. Cross-check against existing probe dumps
    probe_files = {
        "A_production_mirror": out_dir / "01_probe_a_production_mirror.json",
        "B_clean_strip":        out_dir / "02_probe_b_clean_strip.json",
        "C_no_over_yrs":        out_dir / "03_probe_c_no_over_yrs.json",
        "D_pipeline_trace":     out_dir / "04_probe_d_pipeline_trace.json",
    }
    probe_id_sets: Dict[str, set] = {}
    for label, path in probe_files.items():
        if not path.exists():
            probe_id_sets[label] = set()
            continue
        data = json.loads(path.read_text())
        ids: set = set()
        if label == "D_pipeline_trace":
            for t in data.get("all_traces", []):
                if t.get("candidate_id"):
                    ids.add(str(t["candidate_id"]))
        else:
            for p in data.get("pages", []):
                for c in p.get("candidates", []):
                    if c.get("candidate_id"):
                        ids.add(str(c["candidate_id"]))
        probe_id_sets[label] = ids
        print(f"  {label}: {len(ids)} unique candidate IDs")

    # 3. Cross-reference
    cross: Dict[str, Dict[str, Any]] = {}
    for email, info in resolved.items():
        cid = info.get("candidate_id")
        if not cid:
            cross[email] = {"resolved_id": None, "found_in": []}
            continue
        found_in = [label for label, ids in probe_id_sets.items() if cid in ids]
        cross[email] = {"resolved_id": cid, "found_in": found_in, "detail": info}

    dump = {
        "resolved": resolved,
        "probe_id_counts": {k: len(v) for k, v in probe_id_sets.items()},
        "cross_reference": cross,
    }
    (out_dir / "05_probe_e_email_lookup.json").write_text(json.dumps(dump, indent=2, default=str))

    # 4. Append to report.md
    report_path = out_dir / "report.md"
    extra: List[str] = ["\n## Probe E — email → candidate_id resolution\n"]
    extra.append("| Target email | JobDiva candidate_id | Found in A | B | C | D | State | Last modified |")
    extra.append("|---|---|---|---|---|---|---|---|")
    for email in targets:
        c = cross[email]
        cid = c.get("resolved_id")
        d = c.get("detail") or {}
        found = c.get("found_in") or []
        extra.append(
            f"| `{email}` | `{cid or '—'}` | "
            f"{'✓' if 'A_production_mirror' in found else '✗'} | "
            f"{'✓' if 'B_clean_strip' in found else '✗'} | "
            f"{'✓' if 'C_no_over_yrs' in found else '✗'} | "
            f"{'✓' if 'D_pipeline_trace' in found else '✗'} | "
            f"{d.get('detail_state') or '—'} | "
            f"{d.get('detail_last_modified') or '—'} |"
        )

    extra.append("\n### Revised verdict per email\n")
    for email in targets:
        c = cross[email]
        cid = c.get("resolved_id")
        d = c.get("detail") or {}
        found = c.get("found_in") or []
        if not cid:
            verdict = "JobDiva `searchCandidateProfile` could not resolve this email → candidate is not in this JobDiva account (or has a different primary email)."
        elif "A_production_mirror" in found and "D_pipeline_trace" in found:
            verdict = "In TalentSearch + reached pipeline. Re-check report.md target_traces for drop reason."
        elif found:
            verdict = f"In TalentSearch but not in production pipeline. Probes that found it: {found}."
        else:
            verdict = (
                f"Candidate `{cid}` exists in JobDiva (state={d.get('detail_state')}, "
                f"last_modified={d.get('detail_last_modified')}) but is NOT in any of the "
                f"20,750 results returned by JobDiva TalentSearch across A/B/C, nor in the "
                f"572-candidate production pipeline. JobDiva TalentSearch's index does not "
                f"include this candidate — possibly excluded by JobDiva-side criteria not "
                f"in our boolean (recent activity, resume on file, opt-in status, ATS state)."
            )
        extra.append(f"- **`{email}`**: {verdict}")

    with report_path.open("a") as f:
        f.write("\n".join(extra) + "\n")

    print(f"\n✓ Appended Probe E to {report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--target-emails", required=True)
    args = ap.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
