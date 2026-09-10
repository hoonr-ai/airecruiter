"""Resolve a sourced-candidate row to a *person*.

Why this module exists
---------------------
`sourced_candidates.candidate_id` is not a person. It is source-scoped, and for
some sources it used to be run-scoped:

    JobDiva            "1234567"                stable
    Exa people search  "exa_<normalized url>"   stable
    Exa deep search    "exadeep_<normalized url>"  stable (was position-keyed)
    Dice               "dice_<normalized url>"     stable (was position-keyed)
    profiles fallback  "<source>_<md5(email|phone|name)>"  source-prefixed

So one human reached through JobDiva and again through LinkedIn carries two
different `candidate_id`s, and any "have we already contacted them?" rule built
on `candidate_id` alone works for JobDiva applicants and silently misses the
LinkedIn/Exa cohort.

`resolve_person` maps a candidate dict onto a `person_id` using a strict
confidence ladder, recording every key it learns in `candidate_identity_key` so
the next sighting is a single index lookup.

The ladder (highest confidence first)
------------------------------------
1. ``linkedin_url``  — normalized profile URL. A profile URL is one human.
2. ``jobdiva_id``    — the ATS's own person id, when the row came from JobDiva.
3. ``email``         — normalized, with synthetic/placeholder addresses rejected.
4. ``phone``         — last 10 digits (NANP), with obvious junk rejected.
5. ``resume_hash``   — SHA256 of the normalized résumé text, already computed
                       and indexed on `candidate_enhanced_info`.

What is deliberately NOT a key
------------------------------
* **Name**, alone or with location. "John Smith, Dallas TX" is not an identity;
  merging on it would collapse distinct people into one person and suppress
  outreach to someone we have never contacted. Names are stored for display and
  used only to *suggest* a merge to a human.
* **Synthetic JobDiva contacts.** JobDiva emits `Auto_*@jobdiva.com` addresses
  and the literal string "Available upon request" for candidates with no real
  contact info. Keying on those would merge every such candidate into one
  person. See `_is_synthetic_email`.
* **Short/repeated phone digits.** 5555555555 and friends appear as
  placeholders across many rows.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.db import get_db_connection
from services.location import sanitize_candidate_location

logger = logging.getLogger(__name__)


# Ladder order. Also the precedence used to pick `candidate_person.primary_key_type`.
KEY_LADDER: Tuple[str, ...] = ("linkedin_url", "jobdiva_id", "email", "phone", "resume_hash")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# Domains that never identify a person: JobDiva's synthetic mailbox, plus the
# usual test/no-reply sinks.
_SYNTHETIC_EMAIL_DOMAINS = {
    "jobdiva.com",
    "example.com",
    "example.org",
    "test.com",
    "noemail.com",
    "no-email.com",
    "none.com",
    "invalid.com",
    "localhost",
}

# Local-part prefixes JobDiva and scrapers use for generated addresses.
_SYNTHETIC_EMAIL_PREFIXES = ("auto_", "noreply", "no-reply", "donotreply", "do-not-reply")

_PLACEHOLDER_TEXTS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "not provided",
    "not available",
    "available upon request",
    "no email",
    "no phone",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _is_placeholder(value: Any) -> bool:
    return _clean(value).lower() in _PLACEHOLDER_TEXTS


# ---------------------------------------------------------------------------
# Key normalizers
# ---------------------------------------------------------------------------
def normalize_profile_url(url: Any) -> str:
    """Scheme/host-prefix stripped, lowercased, query and fragment dropped,
    trailing slash removed — the same shape `exa_service._normalize_linkedin_url`
    produces, so ids minted there resolve to the same key.

    Kept generic (not LinkedIn-only) so Dice and other profile URLs normalize
    through the same path.
    """
    raw = _clean(url).lower()
    if not raw or _is_placeholder(raw):
        return ""
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^www\.", "", raw)
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    raw = raw.rstrip("/")
    # A bare host with no path is a company page or a dead link, not a person.
    if "/" not in raw:
        return ""
    return raw


def _is_synthetic_email(email: str) -> bool:
    """True when the address is generated rather than the candidate's own.

    JobDiva mints `Auto_<id>@jobdiva.com` for candidates with no contact info,
    so treating those as an identity key would merge every such candidate into a
    single person and suppress outreach to all of them.
    """
    local, _, domain = email.partition("@")
    if domain in _SYNTHETIC_EMAIL_DOMAINS:
        return True
    if any(domain.endswith("." + d) for d in _SYNTHETIC_EMAIL_DOMAINS):
        return True
    return local.startswith(_SYNTHETIC_EMAIL_PREFIXES)


def normalize_email(email: Any) -> str:
    """Lowercased and trimmed, or "" when unusable as an identity key.

    Note: dots and `+tags` are NOT stripped. Gmail treats `a.b@gmail.com` and
    `ab@gmail.com` as one mailbox but most providers do not, so stripping them
    globally would merge distinct people. Under-merging is recoverable (a second
    key on the ladder usually catches it); over-merging suppresses outreach to
    someone we never contacted.
    """
    raw = _clean(email).lower()
    if not raw or _is_placeholder(raw):
        return ""
    if not _EMAIL_RE.match(raw):
        return ""
    if _is_synthetic_email(raw):
        return ""
    return raw


def normalize_phone(phone: Any) -> str:
    """Last 10 digits (NANP subscriber number), or "" when unusable.

    Using the last 10 digits makes "+1 (555) 010-1234", "555-010-1234" and
    "15550101234" the same key. Numbers with fewer than 10 digits, or with a
    single repeated digit, are rejected as placeholders.
    """
    raw = _clean(phone)
    if not raw or _is_placeholder(raw):
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 10:
        return ""
    tail = digits[-10:]
    if len(set(tail)) <= 2:
        # 5555555555, 1111111111, 1212121212 — placeholder patterns.
        return ""
    if tail.startswith("0") or tail.startswith("1"):
        # No NANP area code begins with 0 or 1; this is a malformed capture.
        return ""
    return tail


def normalize_resume_hash(value: Any) -> str:
    raw = _clean(value).lower()
    # SHA256 hex only — guards against a caller passing raw résumé text.
    return raw if re.fullmatch(r"[0-9a-f]{64}", raw) else ""


def compute_resume_hash(resume_text: Any) -> str:
    """SHA256 of whitespace-normalized résumé text.

    Mirrors `sourced_candidates_storage._compute_resume_hash` (same normalization,
    same 50-char floor) so a hash computed either place resolves to the same key.
    """
    raw = _clean(resume_text)
    if not raw:
        return ""
    normalized = re.sub(r"\s+", " ", raw).strip()
    if len(normalized) < 50:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Key extraction
# ---------------------------------------------------------------------------
def _source_is_jobdiva(source: Any) -> bool:
    return "jobdiva" in _clean(source).lower().replace("-", "").replace("_", "")


def _jobdiva_person_id(candidate: Dict[str, Any], data: Dict[str, Any]) -> str:
    """The JobDiva-native candidate id, if this row actually came from JobDiva.

    Only trusted for JobDiva-sourced rows: a LinkedIn row's `candidate_id` is an
    `exa_…` string, which is a fine `source_cid` but is not a JobDiva person id.
    """
    explicit = _clean(
        data.get("jobdiva_candidate_id")
        or candidate.get("jobdiva_candidate_id")
        or data.get("jobdiva_id_candidate")
    )
    if explicit.isdigit():
        return explicit
    if _source_is_jobdiva(candidate.get("source") or data.get("source")):
        cid = _clean(candidate.get("candidate_id") or candidate.get("id"))
        if cid.isdigit():
            return cid
    return ""


def extract_identity_keys(candidate: Dict[str, Any]) -> List[Tuple[str, str, float]]:
    """Return `[(key_type, key_value, confidence), …]` in ladder order.

    Accepts either a `sourced_candidates` row (with a `data` JSONB blob) or a
    live search-result dict; missing pieces are simply skipped.
    """
    data = candidate.get("data")
    if isinstance(data, str):
        import json

        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    if not isinstance(data, dict):
        data = {}

    urls = data.get("urls") if isinstance(data.get("urls"), dict) else {}

    keys: List[Tuple[str, str, float]] = []

    url = normalize_profile_url(
        candidate.get("profile_url")
        or candidate.get("linkedin_url")
        or data.get("profile_url")
        or urls.get("linkedin")
        or urls.get("linkedin_url")
    )
    if url:
        keys.append(("linkedin_url", url, 1.0))

    jd_id = _jobdiva_person_id(candidate, data)
    if jd_id:
        keys.append(("jobdiva_id", jd_id, 1.0))

    email = normalize_email(candidate.get("email") or data.get("email"))
    if email:
        keys.append(("email", email, 0.95))

    phone = normalize_phone(candidate.get("phone") or data.get("phone"))
    if phone:
        keys.append(("phone", phone, 0.85))

    rhash = normalize_resume_hash(
        candidate.get("resume_hash") or data.get("resume_hash")
    ) or compute_resume_hash(candidate.get("resume_text") or data.get("resume_text"))
    if rhash:
        keys.append(("resume_hash", rhash, 0.80))

    return keys


def source_cid_key(candidate: Dict[str, Any]) -> str:
    """The `source_cid` shortcut key: "<source>|<candidate_id>".

    Not an identity signal in itself — it is a memo of "we already resolved this
    exact sourced row", so repeat sightings skip the ladder entirely.
    """
    cid = _clean(candidate.get("candidate_id") or candidate.get("id"))
    if not cid:
        return ""
    source = _clean(candidate.get("source") or "unknown").lower()
    return f"{source}|{cid}"


# ---------------------------------------------------------------------------
# Display fields
# ---------------------------------------------------------------------------
def _display_fields(candidate: Dict[str, Any]) -> Dict[str, Any]:
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else {}
    name = _clean(candidate.get("name") or data.get("candidate_name"))
    return {
        "display_name": name or None,
        "primary_email": normalize_email(candidate.get("email") or data.get("email")) or None,
        "primary_phone": normalize_phone(candidate.get("phone") or data.get("phone")) or None,
        "headline": _clean(candidate.get("headline") or data.get("job_title")) or None,
        # Reuse the shared sanitizer so a work-arrangement string ("Remote",
        # "Hybrid") can never be persisted as this person's location.
        "location": sanitize_candidate_location(
            candidate.get("location") or data.get("current_location")
        ) or None,
        "profile_url": _clean(candidate.get("profile_url") or data.get("profile_url")) or None,
    }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _lookup_persons(cur, keys: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], int]:
    """Batch-resolve `(key_type, key_value)` pairs to person ids."""
    if not keys:
        return {}
    cur.execute(
        """
        SELECT key_type, key_value, person_id
        FROM candidate_identity_key
        WHERE (key_type, key_value) IN (
            SELECT * FROM UNNEST(%s::text[], %s::text[])
        )
        """,
        ([k[0] for k in keys], [k[1] for k in keys]),
    )
    return {(r[0], r[1]): r[2] for r in cur.fetchall() or []}


def _create_person(cur, candidate: Dict[str, Any], primary_key_type: Optional[str]) -> int:
    fields = _display_fields(candidate)
    cur.execute(
        """
        INSERT INTO candidate_person
            (display_name, primary_email, primary_phone, headline, location,
             profile_url, primary_key_type)
        VALUES (%(display_name)s, %(primary_email)s, %(primary_phone)s, %(headline)s,
                %(location)s, %(profile_url)s, %(primary_key_type)s)
        RETURNING person_id
        """,
        {**fields, "primary_key_type": primary_key_type},
    )
    return int(cur.fetchone()[0])


def _enrich_person(cur, person_id: int, candidate: Dict[str, Any]) -> None:
    """Fill in blank person fields from a fresh sighting.

    COALESCE-on-existing so we only ever *add* information: a later row with a
    blank phone must not wipe a phone we already have.
    """
    fields = _display_fields(candidate)
    cur.execute(
        """
        UPDATE candidate_person SET
            display_name  = COALESCE(display_name,  %(display_name)s),
            primary_email = COALESCE(primary_email, %(primary_email)s),
            primary_phone = COALESCE(primary_phone, %(primary_phone)s),
            headline      = COALESCE(headline,      %(headline)s),
            location      = COALESCE(location,      %(location)s),
            profile_url   = COALESCE(profile_url,   %(profile_url)s),
            updated_at    = now()
        WHERE person_id = %(person_id)s
        """,
        {**fields, "person_id": person_id},
    )


def _bind_keys(
    cur,
    person_id: int,
    keys: Iterable[Tuple[str, str, float]],
) -> List[Tuple[str, str, int]]:
    """Attach keys to `person_id`; return keys already owned by someone else.

    `ON CONFLICT DO NOTHING` means a key held by a different person stays put and
    is reported back as a merge candidate. We never silently repoint a key —
    that would rewrite history for whichever person previously owned it.
    """
    conflicts: List[Tuple[str, str, int]] = []
    for key_type, key_value, confidence in keys:
        cur.execute(
            """
            INSERT INTO candidate_identity_key (key_type, key_value, person_id, confidence)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (key_type, key_value) DO NOTHING
            RETURNING person_id
            """,
            (key_type, key_value, person_id, confidence),
        )
        if cur.fetchone() is None:
            cur.execute(
                "SELECT person_id FROM candidate_identity_key WHERE key_type = %s AND key_value = %s",
                (key_type, key_value),
            )
            row = cur.fetchone()
            if row and int(row[0]) != person_id:
                conflicts.append((key_type, key_value, int(row[0])))
    return conflicts


def resolve_person(candidate: Dict[str, Any], *, cur=None) -> Optional[int]:
    """Return the `person_id` for a candidate row, creating one if needed.

    Pass an open cursor to enlist in the caller's transaction (the launch path
    does this so the person, the ledger row and the rollup all commit together).
    Without one, this opens and commits its own connection.

    Returns None only when the row carries no usable key at all — no URL, no
    JobDiva id, no real email, no real phone, no résumé. Such a row cannot be
    safely tied to a person, and callers must treat None as "no reuse
    information available" and fall back to a full screen. Guessing here would
    mean suppressing outreach to the wrong human.
    """
    if cur is not None:
        return _resolve_person_with_cursor(cur, candidate)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            person_id = _resolve_person_with_cursor(own_cur, candidate)
        conn.commit()
        return person_id
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("resolve_person failed for %s: %s", source_cid_key(candidate), e)
        return None
    finally:
        conn.close()


def _resolve_person_with_cursor(cur, candidate: Dict[str, Any]) -> Optional[int]:
    memo_key = source_cid_key(candidate)
    ladder = extract_identity_keys(candidate)

    # Fast path: this exact sourced row was resolved before.
    if memo_key:
        found = _lookup_persons(cur, [("source_cid", memo_key)])
        person_id = found.get(("source_cid", memo_key))
        if person_id:
            _enrich_person(cur, person_id, candidate)
            # Re-bind ladder keys: a row that had no email last time may have
            # been enriched since, and that new key should now point at this
            # person too.
            _log_conflicts(_bind_keys(cur, person_id, ladder), memo_key)
            return person_id

    if not ladder:
        logger.info(
            "candidate_identity: no usable identity key for %s — treating as unknown",
            memo_key or "<no candidate_id>",
        )
        return None

    # Resolve against every ladder key at once, then take the match from the
    # highest-confidence rung. Consulting all rungs (rather than stopping at the
    # first hit) is what surfaces merge candidates: if the URL says person 7 and
    # the email says person 12, those two rows are the same human.
    found = _lookup_persons(cur, [(t, v) for t, v, _ in ladder])
    matches: List[Tuple[str, int]] = []
    for key_type in KEY_LADDER:
        for t, v, _ in ladder:
            if t == key_type and (t, v) in found:
                matches.append((key_type, found[(t, v)]))

    if matches:
        winning_key_type, person_id = matches[0]
        distinct = {pid for _, pid in matches}
        if len(distinct) > 1:
            # Do NOT auto-merge. Two person rows sharing a key is either a real
            # duplicate (harmless to leave: the suppression gate is conservative
            # and each person keeps its own history) or a bad key we should not
            # act on. Log for the reconcile report and pick the highest-confidence
            # rung's person so behaviour is at least deterministic.
            logger.warning(
                "candidate_identity: merge candidate — %s resolves to multiple "
                "persons %s via %s; using %s (key_type=%s)",
                memo_key or "<no candidate_id>",
                sorted(distinct),
                [m[0] for m in matches],
                person_id,
                winning_key_type,
            )
        _enrich_person(cur, person_id, candidate)
    else:
        person_id = _create_person(cur, candidate, ladder[0][0] if ladder else None)

    _log_conflicts(_bind_keys(cur, person_id, ladder), memo_key)
    if memo_key:
        _log_conflicts(
            _bind_keys(cur, person_id, [("source_cid", memo_key, 1.0)]), memo_key
        )
    return person_id


def _log_conflicts(conflicts: Sequence[Tuple[str, str, int]], memo_key: str) -> None:
    for key_type, key_value, other_person in conflicts:
        logger.warning(
            "candidate_identity: key %s=%s already owned by person %s (while "
            "resolving %s) — left in place, review as a merge candidate",
            key_type,
            # Don't log a full email/phone at warning level.
            key_value[:4] + "…" if key_type in ("email", "phone") else key_value,
            other_person,
            memo_key or "<no candidate_id>",
        )


def resolve_persons_bulk(
    candidates: Sequence[Dict[str, Any]], *, cur=None
) -> Dict[str, Optional[int]]:
    """Resolve many candidates, keyed by their `candidate_id`.

    One connection for the whole batch. Used by the launch path (which resolves
    a batch of 75) and by the backfill.
    """
    if not candidates:
        return {}

    def _run(active_cur) -> Dict[str, Optional[int]]:
        out: Dict[str, Optional[int]] = {}
        for candidate in candidates:
            cid = _clean(candidate.get("candidate_id") or candidate.get("id"))
            if not cid:
                continue
            try:
                out[cid] = _resolve_person_with_cursor(active_cur, candidate)
            except Exception as e:  # noqa: BLE001
                # One bad row must not sink the batch: the launch path degrades
                # to "unknown person" for this candidate, which means a full
                # screen — the safe default.
                logger.warning("resolve_persons_bulk: %s failed: %s", cid, e)
                out[cid] = None
        return out

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            result = _run(own_cur)
        conn.commit()
        return result
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.error("resolve_persons_bulk failed: %s", e)
        return {}
    finally:
        conn.close()


def person_ids_for_candidate_ids(candidate_ids: Sequence[str]) -> Dict[str, int]:
    """Look up already-known persons for `candidate_id`s without creating any.

    Read-only counterpart to `resolve_persons_bulk`, for paths that only want to
    know "do we already know this person?" — e.g. the rank list annotating rows.
    Resolves through the `source_cid` memo first, then falls back to matching the
    candidate_id against a `jobdiva_id` key so a JobDiva row still resolves when
    it was first seen under a different source label.
    """
    ids = [_clean(c) for c in candidate_ids if _clean(c)]
    if not ids:
        return {}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT split_part(key_value, '|', 2) AS candidate_id, person_id
                FROM candidate_identity_key
                WHERE key_type = 'source_cid'
                  AND split_part(key_value, '|', 2) = ANY(%s)
                UNION
                SELECT key_value AS candidate_id, person_id
                FROM candidate_identity_key
                WHERE key_type = 'jobdiva_id'
                  AND key_value = ANY(%s)
                """,
                (ids, ids),
            )
            return {r[0]: int(r[1]) for r in cur.fetchall() or []}
    except Exception as e:  # noqa: BLE001
        logger.warning("person_ids_for_candidate_ids failed: %s", e)
        return {}
    finally:
        conn.close()
