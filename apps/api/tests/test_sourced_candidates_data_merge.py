"""Guard: a caller-supplied `data` payload must be MERGED into the stored blob,
never allowed to replace it, and never routed through `jsonb_strip_nulls`.

`sourced_candidates.data` is shared. Callers own the scoring/profile keys they
send, but the backend writes keys no caller carries:

  * `jobdiva_candidate_id` — the person's real JobDiva profile id. Without it
    Launch PAIR sends `link_candidate_id=None`, which is the instruction to
    JobDiva to mint a duplicate "Unknown Unknown" profile.
  * `engage_status` / `engage_interview_id` — the outreach idempotency record.

Every launch calls `/candidates/save` for its selection first (and again on
retry), so a blanket `data = EXCLUDED.data` erased both on the way in — costing
a duplicate JobDiva profile and duplicate outreach.

The second rule exists because the obvious way to write the merge is subtly
wrong. Verified against PostgreSQL 15: carrying a value forward through
`jsonb_strip_nulls(jsonb_build_object(...))` recurses into it and deletes null
members, and `engage_last_response.data` is legitimately null for failed
launches (see the comment at routers/candidates.py:115). Copy stored values
verbatim with `jsonb_each` + `jsonb_object_agg` instead.

These are pinned at the SQL level rather than against a live DB, because the
regressions are single tokens inside string literals and that is exactly what a
future edit is likely to reintroduce.
"""

import io
import re
from pathlib import Path

API_ROOT = Path(__file__).resolve().parent.parent

# Files holding a statement that merges a caller-supplied blob into the table.
UPSERT_FILES = (
    "routers/candidates.py",
    "services/sourced_candidates_storage.py",
    "services/auto_assign_service.py",
)

# Backend-owned keys that no caller payload carries and that must therefore
# survive a merge driven by a caller payload.
PRESERVED_KEYS = ("jobdiva_candidate_id", "engage_status", "engage_interview_id")

# The caller-supplied blob appears as `EXCLUDED.data` in an upsert and as
# `v.data` in the applicant sync's `UPDATE ... FROM (VALUES %s) AS v`.
_CALLER_BLOB_RE = re.compile(r"\b(EXCLUDED|v)\.data\b")
_WIPE_RE = re.compile(r"\bdata\s*=\s*(EXCLUDED|v)\.data\b", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Drop `-- ...` line comments.

    Required, not cosmetic: the merge expressions are documented with comments
    that quote the very anti-patterns this module forbids, so a naive scan of
    the raw text reports the explanation as the offence.
    """
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def _sql_blocks(path: Path):
    """Yield every triple-quoted block in the file (our SQL always lives in one)."""
    s = io.open(path, encoding="utf-8").read()
    i = 0
    while True:
        a = s.find('"""', i)
        if a < 0:
            return
        b = s.find('"""', a + 3)
        if b < 0:
            return
        yield s[a + 3 : b]
        i = b + 3


def _caller_blob_merges():
    """Return [(relpath, comment-stripped SQL)] for each statement that merges a
    caller-supplied blob into `sourced_candidates.data`.

    Keyed on the caller blob rather than on `ON CONFLICT`, so it catches both
    shapes — the three upserts and the applicant sync's
    `UPDATE sourced_candidates AS sc ... FROM (VALUES %s) AS v` — while ignoring
    the many targeted `data = data || %s::jsonb` delta writes, which are fine.
    """
    found = []
    for rel in UPSERT_FILES:
        for block in _sql_blocks(API_ROOT / rel):
            if "sourced_candidates" not in block.lower():
                continue
            body = _strip_sql_comments(block)
            if _CALLER_BLOB_RE.search(body):
                found.append((rel, body))
    return found


def test_merge_sites_are_discovered():
    """Sanity: if this finds nothing, every guard below passes vacuously."""
    sites = _caller_blob_merges()
    assert len(sites) >= 4, (
        f"expected the known caller-blob merge sites, found {len(sites)} — did a "
        "file move, or did the SQL stop living in a triple-quoted block?"
    )


def test_no_merge_site_replaces_the_stored_blob():
    offenders = sorted({rel for rel, sql in _caller_blob_merges() if _WIPE_RE.search(sql)})
    assert not offenders, (
        "these statements replace the whole stored `data` blob with the caller's "
        f"instead of merging, erasing backend-owned keys: {offenders}"
    )


def test_every_merge_site_preserves_backend_owned_keys():
    missing = {}
    for rel, sql in _caller_blob_merges():
        absent = [k for k in PRESERVED_KEYS if k not in sql]
        if absent:
            missing.setdefault(rel, set()).update(absent)
    assert not missing, (
        "merge sites that fail to carry backend-owned keys forward: "
        f"{ {k: sorted(v) for k, v in missing.items()} }"
    )


def test_no_merge_site_routes_preserved_values_through_jsonb_strip_nulls():
    offenders = sorted(
        {rel for rel, sql in _caller_blob_merges() if "jsonb_strip_nulls" in sql}
    )
    assert not offenders, (
        "these merge sites carry preserved values through jsonb_strip_nulls, which "
        f"recursively deletes legitimately-null members inside them: {offenders}"
    )
