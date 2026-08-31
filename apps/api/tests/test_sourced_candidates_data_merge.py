"""Guard: `sourced_candidates.data` must be MERGED on upsert, never replaced.

`data` is a shared jsonb blob. Step 5 owns the scoring/profile keys it sends,
but the backend writes keys the client never sends:

  * `jobdiva_candidate_id` — the person's real JobDiva profile id. Without it
    Launch PAIR sends `link_candidate_id=None`, which is the instruction to
    JobDiva to mint a duplicate "Unknown Unknown" profile.
  * `engage_status` / `engage_interview_id` — the outreach idempotency record.

Every launch calls `/candidates/save` for its selection first (and again on
retry), so a blanket `data = EXCLUDED.data` erased both on the way in — costing
a duplicate JobDiva profile and duplicate outreach.

This pins the invariant at the SQL level rather than mocking a DB, because the
regression is a single token inside a string literal and that is exactly what a
future edit is likely to reintroduce.
"""

import io
import re
from pathlib import Path

API_ROOT = Path(__file__).resolve().parent.parent

# Files holding an INSERT ... ON CONFLICT against sourced_candidates.
UPSERT_FILES = (
    "routers/candidates.py",
    "services/sourced_candidates_storage.py",
    "services/auto_assign_service.py",
)

# Backend-owned keys that no client payload carries and that must therefore
# survive an upsert driven by a client payload.
PRESERVED_KEYS = ("engage_status", "engage_interview_id")

_WIPE_RE = re.compile(r"\bdata\s*=\s*EXCLUDED\.data\b", re.IGNORECASE)
_ASSIGNS_DATA_RE = re.compile(r"\bdata\s*=", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Drop `-- ...` line comments.

    Required, not cosmetic: the merge expressions are documented with comments
    that quote the very anti-pattern this module forbids, so a naive scan of the
    raw text reports the explanation as the offence.
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


def _sourced_candidate_upserts():
    """Return [(relpath, comment-stripped SQL)] for each sourced_candidates upsert."""
    found = []
    for rel in UPSERT_FILES:
        for block in _sql_blocks(API_ROOT / rel):
            lowered = block.lower()
            if "on conflict" in lowered and "sourced_candidates" in lowered:
                found.append((rel, _strip_sql_comments(block)))
    return found


def test_upserts_are_discovered():
    """Sanity: if this finds nothing, the guards below pass vacuously."""
    upserts = _sourced_candidate_upserts()
    assert len(upserts) >= 3, (
        f"expected the known sourced_candidates upserts, found {len(upserts)} — "
        "did a file move, or did the SQL stop living in a triple-quoted block?"
    )


def test_no_upsert_replaces_the_data_blob():
    offenders = sorted({rel for rel, sql in _sourced_candidate_upserts() if _WIPE_RE.search(sql)})
    assert not offenders, (
        "these upserts replace the whole `data` blob instead of merging it, "
        f"which erases backend-owned keys: {offenders}"
    )


def test_every_upsert_preserves_backend_owned_keys():
    missing = {}
    for rel, sql in _sourced_candidate_upserts():
        if not _ASSIGNS_DATA_RE.search(sql):
            continue  # this upsert doesn't touch `data` at all
        absent = [k for k in PRESERVED_KEYS if k not in sql]
        if absent:
            missing.setdefault(rel, set()).update(absent)
    assert not missing, (
        "upserts that write `data` without carrying backend-owned keys forward: "
        f"{ {k: sorted(v) for k, v in missing.items()} }"
    )


def test_jobdiva_profile_link_survives_the_client_save_path():
    """`/candidates/save` is the upsert every launch hits before engaging."""
    blocks = [sql for rel, sql in _sourced_candidate_upserts() if rel == "routers/candidates.py"]
    assert blocks, "no sourced_candidates upsert found in routers/candidates.py"
    for sql in blocks:
        assert "jobdiva_candidate_id" in sql, (
            "the /candidates/save upsert must carry `jobdiva_candidate_id` forward — "
            "losing it makes JobDiva mint a duplicate profile on the next provision"
        )
