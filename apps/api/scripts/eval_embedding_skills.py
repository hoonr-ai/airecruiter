"""Offline eval harness for the embedding-based skill matcher.

Run from `apps/api/`:

    EMBEDDING_SKILL_MATCH=on python -m scripts.eval_embedding_skills

Reads `scripts/eval_embedding_skills.cases.json` (sibling file). Each
case is `{query, candidate_skills, expected_match}`. Prints per-case
keyword score, embedding-augmented score, and a summary precision/recall
delta vs. the keyword-only baseline.

Use this before flipping `EMBEDDING_SKILL_MATCH=on` in any environment —
the goal is to catch a regression where the embedding path lifts the
score on cases that *should* have stayed low (false positives), not just
to confirm it lifts the right cases (true positives).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List

# Allow running as `python -m scripts.eval_embedding_skills` from apps/api/
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import skill_embeddings  # noqa: E402
from services.unified_candidate_search import UnifiedCandidateSearch  # noqa: E402


CASES_PATH = Path(__file__).parent / "eval_embedding_skills.cases.json"


def _load_cases() -> List[Dict]:
    if not CASES_PATH.exists():
        # Bootstrap minimal default cases so the script runs out of the box.
        default = [
            # ── IT positives ──
            {"query": "React", "skills": ["ReactJS", "JavaScript"], "expected": True},
            {"query": "Node.js", "skills": ["Node", "Express"], "expected": True},
            {"query": "Web Development", "skills": ["Web Dev", "HTML/CSS"], "expected": True},
            {"query": "Python", "skills": ["python3", "Django"], "expected": True},
            {"query": "Machine Learning", "skills": ["ML Engineering", "PyTorch"], "expected": True},
            {"query": "AWS", "skills": ["Amazon Web Services", "S3"], "expected": True},

            # ── Program/Project Management positives ──
            {"query": "Stakeholder Management", "skills": ["Executive Alignment", "Cross-functional Coordination"], "expected": True},
            {"query": "Risk Register", "skills": ["RAID Log", "Risk Tracking"], "expected": True},
            {"query": "OKR Tracking", "skills": ["OKRs", "Quarterly Goal Setting"], "expected": True},
            {"query": "Dependency Mapping", "skills": ["Dependency Tracking", "Cross-team Dependencies"], "expected": True},

            # ── Sales positives ──
            {"query": "Account Management", "skills": ["Strategic Account Management", "Customer Relationship Management"], "expected": True},
            {"query": "Pipeline Forecasting", "skills": ["Sales Forecasting", "Pipeline Management"], "expected": True},
            {"query": "MEDDIC", "skills": ["MEDDPICC", "Sales Qualification"], "expected": True},

            # ── Finance / Accounting positives ──
            {"query": "Variance Analysis", "skills": ["Budget Variance", "Forecast vs Actual Analysis"], "expected": True},
            {"query": "Month-End Close", "skills": ["MEC", "Period-End Close Process"], "expected": True},
            {"query": "GL Reconciliation", "skills": ["General Ledger Reconciliation", "Balance Sheet Reconciliation"], "expected": True},

            # ── HR / Marketing / CS positives ──
            {"query": "Employee Relations", "skills": ["ER Cases", "Workplace Investigations"], "expected": True},
            {"query": "Demand Generation", "skills": ["Demand Gen", "Lead Generation Programs"], "expected": True},
            {"query": "Net Revenue Retention", "skills": ["NRR", "Customer Expansion Revenue"], "expected": True},

            # ── Healthcare / Legal positives ──
            {"query": "Patient Care", "skills": ["Clinical Patient Care", "Bedside Care"], "expected": True},
            {"query": "Contract Negotiation", "skills": ["Commercial Contract Negotiation", "Redlining"], "expected": True},

            # ── Negatives — these should NOT match (mostly cross-domain noise). ──
            {"query": "React", "skills": ["Java", "Spring Boot"], "expected": False},
            {"query": "Python", "skills": ["JavaScript", "TypeScript"], "expected": False},
            {"query": "Frontend", "skills": ["DBA", "PL/SQL"], "expected": False},
            {"query": "MEDDIC", "skills": ["Kubernetes", "Helm"], "expected": False},
            {"query": "Variance Analysis", "skills": ["Pipeline Engineering", "Oil & Gas Operations"], "expected": False},
            {"query": "Stakeholder Management", "skills": ["Stakeholder Reporting (PowerBI)", "ETL Pipelines"], "expected": False},
        ]
        CASES_PATH.write_text(json.dumps(default, indent=2))
        print(f"Bootstrapped default cases → {CASES_PATH}")
        return default
    return json.loads(CASES_PATH.read_text())


def _keyword_score(query: str, skills: List[str]) -> float:
    """Mirror of the keyword-overlap path in `_fuzzy_term_score`."""
    u = UnifiedCandidateSearch.__new__(UnifiedCandidateSearch)
    profile = {
        "skills": skills,
        "text": " ".join(skills).lower(),
    }
    return u._fuzzy_term_score(profile, query, "skills")


async def main() -> None:
    if not os.environ.get("EMBEDDING_SKILL_MATCH"):
        os.environ["EMBEDDING_SKILL_MATCH"] = "on"

    cases = _load_cases()
    if not cases:
        print("No cases — exiting.")
        return

    # Pre-warm everything in one batch.
    all_terms = []
    for c in cases:
        all_terms.append(c["query"])
        all_terms.extend(c["skills"])
    await skill_embeddings.warm_terms(all_terms)
    print(f"Warm done. cache_size={skill_embeddings.cache_size()}")

    THRESHOLD = float(os.environ.get("EMBEDDING_MATCH_THRESHOLD", "0.75"))
    tp_kw = fp_kw = fn_kw = tn_kw = 0
    tp_emb = fp_emb = fn_emb = tn_emb = 0

    for c in cases:
        q, skills, expected = c["query"], c["skills"], c["expected"]
        kw = _keyword_score(q, skills)
        cosine = skill_embeddings.best_cosine(q, skills)
        emb = max(kw, cosine if cosine >= THRESHOLD else 0.0)

        kw_match = kw >= 0.5
        emb_match = emb >= 0.5

        if expected and kw_match:
            tp_kw += 1
        elif expected and not kw_match:
            fn_kw += 1
        elif not expected and kw_match:
            fp_kw += 1
        else:
            tn_kw += 1

        if expected and emb_match:
            tp_emb += 1
        elif expected and not emb_match:
            fn_emb += 1
        elif not expected and emb_match:
            fp_emb += 1
        else:
            tn_emb += 1

        flag = "✓" if (expected == emb_match) else "✗"
        print(
            f"{flag} q={q!r:25} expected={expected!s:5} "
            f"kw={kw:.2f} cosine={cosine:.2f} emb={emb:.2f} "
            f"skills={skills}"
        )

    def pr(tp, fp, fn):
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        return p, r

    p_kw, r_kw = pr(tp_kw, fp_kw, fn_kw)
    p_emb, r_emb = pr(tp_emb, fp_emb, fn_emb)
    print()
    print(f"Keyword baseline:   precision={p_kw:.2f} recall={r_kw:.2f} (tp={tp_kw} fp={fp_kw} fn={fn_kw} tn={tn_kw})")
    print(f"Embedding augment:  precision={p_emb:.2f} recall={r_emb:.2f} (tp={tp_emb} fp={fp_emb} fn={fn_emb} tn={tn_emb})")
    print()
    if p_emb < p_kw:
        print("⚠ precision regressed — review false positives before enabling in prod.")
    if r_emb > r_kw and p_emb >= p_kw:
        print("✓ recall improved with no precision regression.")


if __name__ == "__main__":
    asyncio.run(main())
