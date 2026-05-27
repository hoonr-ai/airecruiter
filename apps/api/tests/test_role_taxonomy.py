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

from services.role_taxonomy import expand_title  # noqa: E402


def _titles(input_title: str) -> list[str]:
    return [e["title"] for e in expand_title(input_title, max_results=30)]


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

    if failures:
        print(f"\nFAIL: {failures} check(s) failed")
        return 1
    print("\nOK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
