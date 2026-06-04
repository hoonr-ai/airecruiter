"""Tests for role_taxonomy.expand_title relevance.

Covers the four Step 5 "similar titles" failure modes that motivated the fix:
generic titles returning NO MATCH, fuzzy resolver picking a niche K17000 leaf,
taxonomy data mapping opposite roles (Backend → Frontend), and noisy
K10000/K5000 siblings leaking through with no per-sibling gate.

Run standalone:
    cd apps/api && python -m tests.test_role_taxonomy
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.role_taxonomy import (  # noqa: E402
    _context_tokens,
    _distinctive_tokens_supported,
    compare,
    expand_title,
    expand_title_grounded,
    is_grounded_variant,
)


def _titles(input_title: str) -> list[str]:
    return [e["title"] for e in expand_title(input_title, max_results=30)]


def _grounded(input_title: str, context: str) -> list[str]:
    return [e["title"] for e in expand_title_grounded(input_title, context, max_results=30)]


def _check(name: str, ok: bool, detail: str = "") -> int:
    if ok:
        print(f"PASS: {name}")
        return 0
    print(f"FAIL: {name}: {detail}")
    return 1


def run() -> int:
    failures = 0

    # 1. Generic title that wasn't a K17000 leaf used to return [] (NO MATCH).
    titles = _titles("Software Engineer")
    failures += _check(
        "Software Engineer: returns >= 5 results",
        len(titles) >= 5,
        f"got {len(titles)}: {titles[:3]}",
    )
    failures += _check(
        "Software Engineer: every result is software/engineering-flavoured",
        all(any(k in t.lower() for k in ("software", "engineer", "developer")) for t in titles),
        f"offenders: {[t for t in titles if not any(k in t.lower() for k in ('software', 'engineer', 'developer'))]}",
    )

    # 2. Backend Engineer used to return a Frontend Developer family because
    # the taxonomy maps Backend Engineer's K10000/K5000 to Frontend Developer.
    titles = _titles("Backend Engineer")
    failures += _check(
        "Backend Engineer: no Frontend titles",
        not any("frontend" in t.lower() or "front end" in t.lower() or "front-end" in t.lower() for t in titles),
        f"got: {titles}",
    )

    # 3. Product Manager used to pull niche industry PMs as top results
    # (Telecom PM, E-commerce Product Lead, Product Marketing Engineer).
    titles_top5 = _titles("Product Manager")[:5]
    NICHE_WORDS = ("telecom", "ecommerce", "e-commerce", "marketing engineer")
    failures += _check(
        "Product Manager: no niche industry titles in top 5",
        not any(any(n in t.lower() for n in NICHE_WORDS) for t in titles_top5),
        f"got: {titles_top5}",
    )

    # 4. Project Manager used to pull Coordinator roles from the IT PM K5000.
    titles_top10 = _titles("Project Manager")[:10]
    failures += _check(
        "Project Manager: no Coordinator titles in top 10",
        not any("coordinator" in t.lower() for t in titles_top10),
        f"got: {titles_top10}",
    )
    failures += _check(
        "Project Manager: >= 5 results (the IT PM K10000 family backfills)",
        len(_titles("Project Manager")) >= 5,
        f"got {len(_titles('Project Manager'))}",
    )

    # 5. Empty / garbage input returns no results, doesn't crash.
    failures += _check("empty input returns []", _titles("") == [])
    failures += _check("garbage input returns []", _titles("zxqwfgh nrkpfm") == [])

    # 6. Regression — Front End Developer must not return loan-family titles
    # (the c30f280 regression vector).
    titles = _titles("Front End Developer")
    LOAN_WORDS = ("loan", "mortgage", "lending", "underwriting")
    failures += _check(
        "Front End Developer regression: no loan-family titles",
        not any(any(lw in t.lower() for lw in LOAN_WORDS) for t in titles),
        f"got: {titles}",
    )

    # 7. Regression — Java Developer must keep returning Java-flavoured siblings.
    titles = _titles("Java Developer")
    java_count = sum(1 for t in titles if "java" in t.lower())
    failures += _check(
        "Java Developer: returns >= 3 Java-* titles",
        java_count >= 3,
        f"got {java_count} java titles in {titles}",
    )

    # 8. Input itself isn't recommended back to the user.
    failures += _check(
        "Java Developer: 'Java Developer' not in its own results",
        "Java Developer".lower() not in {t.lower() for t in titles},
        f"got: {titles}",
    )

    # 9. Qualifier stripping — Sr / Senior / Lead should resolve like the bare title.
    sr = _titles("Sr. Java Developer")
    bare = _titles("Java Developer")
    failures += _check(
        "Sr. Java Developer resolves to the same family as Java Developer",
        set(sr) == set(bare),
        f"sr={sr}\nbare={bare}",
    )

    # --- Grounded expansion: similar titles must relate to THIS job's domain ---

    # 10. "Business Analyst" in a HEALTHCARE JD must NOT surface the off-domain
    # finance/robotics family the context-free expand_title returns.
    hc_ctx = ("clinical workflows EMR Epic patient care HIPAA healthcare hospital "
              "claims revenue cycle data reporting")
    hc = _grounded("Business Analyst", hc_ctx)
    BAD = ("mortgage", "robotics", "payment", "fleet", "licensing", "sports",
           "telecom", "agriculture", "mining")
    failures += _check(
        "Healthcare BA: no off-domain (mortgage/robotics/payment/...) variants",
        not any(any(b in t.lower() for b in BAD) for t in hc),
        f"got: {hc}",
    )

    # 11. "Business Analyst" in a MORTGAGE/LENDING JD keeps the mortgage variant
    # but still drops robotics/sports.
    loan_ctx = "mortgage lending underwriting loan origination FHA servicing escrow"
    loan = _grounded("Business Analyst", loan_ctx)
    failures += _check(
        "Mortgage BA: includes a mortgage/lending variant",
        any("mortgage" in t.lower() or "lending" in t.lower() for t in loan),
        f"got: {loan}",
    )
    failures += _check(
        "Mortgage BA: still excludes robotics/sports variants",
        not any("robotics" in t.lower() or "sports" in t.lower() for t in loan),
        f"got: {loan}",
    )

    # 12. Empty context => tight set, never the full ~30-member noisy family.
    empty_ctx = _grounded("Business Analyst", "")
    full_family = _titles("Business Analyst")
    failures += _check(
        "Empty-context BA: far fewer than the context-free family",
        len(empty_ctx) <= 5 and len(empty_ctx) < len(full_family),
        f"empty={len(empty_ctx)} full={len(full_family)}: {empty_ctx}",
    )

    # 13. _distinctive_tokens_supported unit rule (strict AND, exact match).
    hc_tokens = _context_tokens("healthcare hospital clinical patient")
    failures += _check(
        "distinctive: Mortgage BA NOT supported in a healthcare context",
        not _distinctive_tokens_supported("Business Analyst", "Mortgage Business Analyst", hc_tokens),
    )
    failures += _check(
        "distinctive: zero-distinctive variant (Senior Business Analyst) supported",
        _distinctive_tokens_supported("Business Analyst", "Senior Business Analyst", _context_tokens("")),
    )

    # 14. is_grounded_variant closes the K5000-family hole: an off-domain LLM
    # title is rejected even though compare() puts it in the same family.
    failures += _check(
        "K5000 hole closed: compare() treats Mortgage BA as related to BA",
        compare("Business Analyst", "Mortgage Business Analyst") != "none",
        f"compare returned {compare('Business Analyst', 'Mortgage Business Analyst')!r}",
    )
    failures += _check(
        "is_grounded_variant: rejects off-domain Mortgage BA in a healthcare JD",
        not is_grounded_variant("Business Analyst", "Mortgage Business Analyst", hc_ctx),
    )
    failures += _check(
        "is_grounded_variant: accepts on-domain Healthcare BA in a healthcare JD",
        is_grounded_variant("Business Analyst", "Healthcare Business Analyst", hc_ctx),
    )

    # 15. Regression: legacy expand_title behaviour is unchanged.
    failures += _check(
        "expand_title (legacy) still returns >= 5 for Software Engineer",
        len(_titles("Software Engineer")) >= 5,
    )

    if failures:
        print(f"\nFAIL: {failures} check(s) failed")
        return 1
    print("\nOK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
