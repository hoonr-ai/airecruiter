"""Compare baseline vs lenient sourcing_debug runs.

Reads two output directories (`--baseline` and `--lenient`) produced by
`sourcing_debug.py` and prints a diff:
  - Probe-by-probe candidate counts
  - Stage-by-stage drop counts (Probe D)
  - New candidates that the lenient run surfaced
  - Whether the target emails appeared in either run
  - The active flags that were toggled
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


def _load(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _candidate_id_set(probe: Optional[Dict[str, Any]], key: str = "pages") -> Set[str]:
    """Collect candidate_ids from a probe dump. `key='pages'` for probes A/B/C/F,
    `'all_traces'` for Probe D."""
    if not probe:
        return set()
    ids: Set[str] = set()
    if key == "pages":
        for p in probe.get("pages", []):
            for c in p.get("candidates", []):
                cid = c.get("candidate_id")
                if cid:
                    ids.add(str(cid))
        for c in probe.get("candidates_preview", []):
            cid = c.get("candidate_id")
            if cid:
                ids.add(str(cid))
        for cid in probe.get("candidate_ids", []) or []:
            if cid:
                ids.add(str(cid))
    elif key == "all_traces":
        for t in probe.get("all_traces", []):
            cid = t.get("candidate_id")
            if cid:
                ids.add(str(cid))
    return ids


def _stage_counts_for_d(probe_d: Optional[Dict[str, Any]]) -> Dict[str, int]:
    if not probe_d:
        return {}
    traces = probe_d.get("all_traces", [])
    counts: Dict[str, int] = {
        "raw": len(traces),
        "stage1_kept": int(probe_d.get("stage1_kept") or 0),
        "stage1_dropped": int(probe_d.get("stage1_dropped") or 0),
        "stage2_below_min_years_pre_llm": 0,
        "stage3a_filter_assessment_pre_failed": 0,
        "stage5_filter_assessment_post_failed": 0,
        "stage5_min_years_failure": 0,
        "reached_ui": 0,
    }
    for t in traces:
        ds = t.get("drop_stage")
        if ds == "stage2_pre_llm_yoe":
            counts["stage2_below_min_years_pre_llm"] += 1
        elif ds == "stage3a_filter_assessment_pre":
            counts["stage3a_filter_assessment_pre_failed"] += 1
        elif ds in ("stage5_filter_assessment_post", "stage5_post_yoe_floor"):
            counts["stage5_filter_assessment_post_failed"] += 1
            stage5 = t.get("stage5") or {}
            if stage5.get("min_years_failure"):
                counts["stage5_min_years_failure"] += 1
        elif ds is None:
            counts["reached_ui"] += 1
    return counts


def _compare(baseline: Path, lenient: Path) -> int:
    probes = {
        "A_production_mirror": "01_probe_a_production_mirror.json",
        "B_clean_strip": "02_probe_b_clean_strip.json",
        "C_no_over_yrs": "03_probe_c_no_over_yrs.json",
        "D_pipeline_trace": "04_probe_d_pipeline_trace.json",
        "F_jobagent": "06_probe_f_jobagent.json",
        "G_full_state_name": "07_probe_g_full_state_name.json",
    }

    lines: List[str] = []
    lines.append(f"# Sourcing-debug comparison: baseline vs lenient\n")
    lines.append(f"- baseline dir: `{baseline}`")
    lines.append(f"- lenient dir:  `{lenient}`")

    base_flags = _load(baseline / "_active_flags.json") or {}
    lent_flags = _load(lenient / "_active_flags.json") or {}
    if base_flags or lent_flags:
        lines.append("\n## Flag deltas")
        keys = sorted(set(base_flags.keys()) | set(lent_flags.keys()))
        lines.append("| flag | baseline | lenient |")
        lines.append("|---|---|---|")
        for k in keys:
            lines.append(f"| `{k}` | `{base_flags.get(k, '—')}` | `{lent_flags.get(k, '—')}` |")

    lines.append("\n## Per-probe candidate counts")
    lines.append("| probe | baseline | lenient | Δ |")
    lines.append("|---|---|---|---|")
    base_ids_by_probe: Dict[str, Set[str]] = {}
    lent_ids_by_probe: Dict[str, Set[str]] = {}
    for label, fname in probes.items():
        b = _load(baseline / fname)
        l = _load(lenient / fname)
        key = "all_traces" if label == "D_pipeline_trace" else "pages"
        b_ids = _candidate_id_set(b, key)
        l_ids = _candidate_id_set(l, key)
        base_ids_by_probe[label] = b_ids
        lent_ids_by_probe[label] = l_ids
        delta = len(l_ids) - len(b_ids)
        lines.append(
            f"| {label} | {len(b_ids)} | {len(l_ids)} | "
            f"{'+' if delta >= 0 else ''}{delta} |"
        )

    # Pipeline-stage drop breakdown
    b_d_counts = _stage_counts_for_d(_load(baseline / probes["D_pipeline_trace"]))
    l_d_counts = _stage_counts_for_d(_load(lenient / probes["D_pipeline_trace"]))
    lines.append("\n## Probe D — pipeline stage breakdown")
    lines.append("| stage | baseline | lenient | Δ |")
    lines.append("|---|---|---|---|")
    for k in [
        "raw",
        "stage1_kept",
        "stage1_dropped",
        "stage2_below_min_years_pre_llm",
        "stage3a_filter_assessment_pre_failed",
        "stage5_filter_assessment_post_failed",
        "stage5_min_years_failure",
        "reached_ui",
    ]:
        b = b_d_counts.get(k, 0)
        l = l_d_counts.get(k, 0)
        delta = l - b
        lines.append(
            f"| `{k}` | {b} | {l} | "
            f"{'+' if delta >= 0 else ''}{delta} |"
        )

    # New candidates surfaced in lenient (D probe)
    new_in_lenient = lent_ids_by_probe.get("D_pipeline_trace", set()) - base_ids_by_probe.get("D_pipeline_trace", set())
    lost_in_lenient = base_ids_by_probe.get("D_pipeline_trace", set()) - lent_ids_by_probe.get("D_pipeline_trace", set())
    lines.append(f"\n## Probe D — set diff")
    lines.append(f"- new in lenient: **{len(new_in_lenient)}**")
    lines.append(f"- lost in lenient: **{len(lost_in_lenient)}**")
    lines.append(f"- intersection: **{len(lent_ids_by_probe.get('D_pipeline_trace', set()) & base_ids_by_probe.get('D_pipeline_trace', set()))}**")

    # Reached-UI delta — the most important metric
    b_d = _load(baseline / probes["D_pipeline_trace"])
    l_d = _load(lenient / probes["D_pipeline_trace"])
    b_passed = {t["candidate_id"] for t in (b_d.get("all_traces", []) if b_d else []) if not t.get("drop_stage")}
    l_passed = {t["candidate_id"] for t in (l_d.get("all_traces", []) if l_d else []) if not t.get("drop_stage")}
    extra_passed = l_passed - b_passed
    lost_passed = b_passed - l_passed
    lines.append(f"\n## Reached-UI (passed all stages) delta")
    lines.append(f"- baseline reached UI: **{len(b_passed)}**")
    lines.append(f"- lenient reached UI:  **{len(l_passed)}**")
    lines.append(f"- newly reached UI:    **{len(extra_passed)}**")
    lines.append(f"- newly dropped:       **{len(lost_passed)}**")

    if extra_passed and l_d:
        lines.append("\n### Sample of newly-reached-UI candidates (first 20)")
        lines.append("| id | name | state | title | source | baseline drop |")
        lines.append("|---|---|---|---|---|---|")
        b_drop_by_id: Dict[str, str] = {}
        if b_d:
            for t in b_d.get("all_traces", []):
                cid = t.get("candidate_id")
                if cid:
                    b_drop_by_id[cid] = str(t.get("drop_stage") or "—")
        l_by_id = {t["candidate_id"]: t for t in l_d.get("all_traces", []) if t.get("candidate_id")}
        for cid in list(extra_passed)[:20]:
            t = l_by_id.get(cid, {})
            lines.append(
                f"| `{cid}` | {t.get('name','')} | {t.get('state','')} | "
                f"{(t.get('title','') or '')[:35]} | "
                f"{(t.get('source','') or '')[:25]} | `{b_drop_by_id.get(cid, 'not in pool')}` |"
            )

    # Probe F context — JobAgent
    f_base = _load(baseline / probes["F_jobagent"])
    f_lent = _load(lenient / probes["F_jobagent"])
    if f_base or f_lent:
        lines.append("\n## Probe F — JobAgentSearch (+ pipeline trace)")
        lines.append("| run | raw | stage1 | reached UI | target hits | criteria_unconfigured |")
        lines.append("|---|---|---|---|---|---|")
        for label, f in [("baseline", f_base), ("lenient", f_lent)]:
            if not f:
                lines.append(f"| {label} | — | — | — | — | (probe not run) |")
                continue
            if f.get("error"):
                lines.append(f"| {label} | — | — | — | — | ERROR: {f['error']} |")
                continue
            hits = f.get("target_hits") or {}
            total_targets = len(hits) + len(f.get("missing_targets", []))
            lines.append(
                f"| {label} | {f.get('candidate_count')} | {f.get('stage1_kept', '—')} | "
                f"{f.get('reached_ui_count', '—')} | {len(hits)}/{total_targets} | "
                f"{f.get('criteria_unconfigured')} |"
            )
        # Target email drop stages in lenient
        if f_lent and f_lent.get("target_traces"):
            lines.append("\n### Lenient Probe F — target email pipeline stages")
            lines.append("| email | candidate_id | state | drop_stage | reason |")
            lines.append("|---|---|---|---|---|")
            for email, tr in (f_lent.get("target_traces") or {}).items():
                if tr is None:
                    lines.append(f"| `{email}` | — | — | not in JobAgent response | — |")
                    continue
                drop = tr.get("drop_stage") or "**reached UI**"
                reason = tr.get("drop_reason") or "—"
                lines.append(
                    f"| `{email}` | `{tr.get('candidate_id','')}` | {tr.get('state','')} | "
                    f"`{drop}` | {reason} |"
                )

    # Probe G — full state name
    g_base = _load(baseline / probes["G_full_state_name"])
    g_lent = _load(lenient / probes["G_full_state_name"])
    if g_base or g_lent:
        lines.append("\n## Probe G — `states='New Jersey'` instead of `'NJ'`")
        lines.append("| run | total candidates | unique IDs | target hits |")
        lines.append("|---|---|---|---|")
        for label, g in [("baseline", g_base), ("lenient", g_lent)]:
            if not g:
                lines.append(f"| {label} | — | — | (probe not run) |")
                continue
            ids = _candidate_id_set(g, "pages")
            hits = g.get("target_hits") or {}
            total_targets = len(hits) + len(g.get("missing_targets", []))
            lines.append(
                f"| {label} | {g.get('total_candidates')} | {len(ids)} | {len(hits)}/{total_targets} |"
            )

    out_path = lenient / "comparison.md"
    out_path.write_text("\n".join(lines) + "\n")
    print(f"\n✓ Wrote {out_path}")
    print("\n".join(lines[-25:]))  # tail to stdout for quick scan
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--lenient", required=True)
    args = ap.parse_args()
    return _compare(Path(args.baseline).resolve(), Path(args.lenient).resolve())


if __name__ == "__main__":
    sys.exit(main())
