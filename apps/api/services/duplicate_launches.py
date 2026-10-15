"""Diagnostic: people launched more than once for one job under DIFFERENT
candidate_ids — the duplicate launches the launch claims' person gate
(routers/engagement._person_gate) now prevents, from before it existed.

Every "launched" metric counts candidate_ids, so each such person counted as
two (or more) launched candidates and was called / emailed that many times.
Those were real launches, so the counts stay candidate_id-grain; this read
sizes them instead (GET /api/v1/admin/duplicate-launches).

A "person" is the person keys of services/launch_person_keys.py (email,
phone, LinkedIn, JobDiva id), joined transitively within a job.
"""

from __future__ import annotations

from typing import Any, Dict, List

from services import launch_person_keys as lpk

# Launched rows (an interview id) with their contacts, keyed by the job's
# canonical monitored_jobs row whichever key the row was stored under.
_LAUNCHED_ROWS_SQL = """
    SELECT COALESCE(m.job_id::text, sc.jobdiva_id) AS job_key,
           NULLIF(m.jobdiva_id, '') AS jobdiva_ref,
           sc.candidate_id, sc.source,
           COALESCE(NULLIF(sc.email, ''), sc.data->>'email') AS email,
           COALESCE(NULLIF(sc.phone, ''), sc.data->>'phone') AS phone,
           COALESCE(NULLIF(sc.profile_url, ''), sc.data->>'profile_url',
                    sc.data->>'linkedin_url', sc.data->'urls'->>'linkedin') AS profile_url,
           sc.data->>'jobdiva_candidate_id' AS jobdiva_candidate_id,
           sc.data->>'engage_interview_id' AS interview_id
    FROM sourced_candidates sc
    LEFT JOIN LATERAL (
        SELECT mj.job_id, mj.jobdiva_id FROM monitored_jobs mj
        WHERE mj.jobdiva_id = sc.jobdiva_id OR mj.job_id::text = sc.jobdiva_id
        LIMIT 1
    ) m ON TRUE
    WHERE COALESCE(sc.data->>'engage_interview_id', '') <> ''
"""


def group_duplicates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per job, the people (union of person keys) launched under 2+ ids."""
    by_job: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_job.setdefault(str(r.get("job_key") or ""), []).append(r)
    out: List[Dict[str, Any]] = []
    for job_key, job_rows in by_job.items():
        parent: Dict[str, str] = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        owner: Dict[str, str] = {}
        kinds: Dict[str, set] = {}
        cids = set()
        for r in job_rows:
            cid = str(r.get("candidate_id") or "")
            if not cid:
                continue
            cids.add(cid)
            find(cid)
            row = {**r, "data": {"jobdiva_candidate_id": r.get("jobdiva_candidate_id")}}
            for k in lpk.person_keys(row):
                if k in owner and find(owner[k]) != find(cid):
                    kinds.setdefault(cid, set()).add(lpk.key_kind(k))
                    parent[find(cid)] = find(owner[k])
                owner.setdefault(k, cid)
        groups: Dict[str, List[str]] = {}
        for cid in cids:
            groups.setdefault(find(cid), []).append(cid)
        for members in groups.values():
            if len(members) < 2:
                continue
            members.sort()
            out.append({
                "job_id": job_key,
                "jobdiva_id": next((r.get("jobdiva_ref") for r in job_rows if r.get("jobdiva_ref")), None),
                "candidate_ids": members,
                "matched_by": sorted(set().union(*(kinds.get(c, set()) for c in members))),
                "extra_launches": len(members) - 1,
            })
    out.sort(key=lambda d: (-d["extra_launches"], d["job_id"]))
    return out


def find_duplicate_launches(conn, *, limit: int = 200) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30000ms'")
        cur.execute(_LAUNCHED_ROWS_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    groups = group_duplicates(rows)
    return {
        "jobs_affected": len({g["job_id"] for g in groups}),
        "people_affected": len(groups),
        "extra_launches": sum(g["extra_launches"] for g in groups),
        "groups": groups[: max(1, int(limit))],
    }
