"""Who is "the same person" for the launch gate: never post one human twice
for one job, whatever ``candidate_id`` each copy carries.

``sourced_candidates.candidate_id`` is source-scoped, not a person: one human
can be a JobDiva numeric id, ``exa_<linkedin-url>``, a Unipile ``AEMAA…`` id
and a ``manual_<ts>_<md5>`` upload at once. Every launch gate used to key on
that id (plus ``source:id`` and the ``data.jobdiva_candidate_id`` twin), so a
person launched as one id was launched again — a second call, a second
interview, and a second "launched" in every metric — as soon as a later
search found them under another.

The gate matches on contacts, because the harm is contact-level: PAIR must
never email the same address or dial the same number twice for one job — and a
copy that shares any one of them with a person already held for the job is
that person. A row's person keys (``person_keys``):

  email:<address>     trimmed, lower-cased; placeholders excluded by the
                      backend's one rule (utils.email_utils.is_placeholder_email:
                      no "@", ``*@jobdiva.com`` incl. ``Auto_*``, ``*.local``
                      such as the ``pair-…@no-email.jobdiva.local`` minted for
                      phone-only people, ``*@noemail.pair.ai``, example.com …)
  phone:<last 10>     digits only; fewer than 10 digits or fewer than 4
                      distinct digits (000-000-0000, 555-555-5555) excluded
  linkedin:<slug>     the ``/in/<slug>`` (or ``/pub/``) of any LinkedIn URL on
                      the row; recruiter-seat ``/talent/`` links are opaque
                      and excluded
  jobdiva:<id>        ``data.jobdiva_candidate_id`` (the profile PAIR created
                      or linked), or the id of a JobDiva-sourced row

Gmail dot/``+tag`` folding is deliberately NOT done (only Gmail collapses
those) and a name is never a key: over-merging silently suppresses outreach
to someone never contacted.

``find_person_duplicates`` is the decision the launch claims run
(routers/engagement.py ``_person_gate``) and the selection mirrors
(services/launch_sessions ``launched_person``; apps/web/lib/launch-rules.ts
``personKeys``) — keep the three in step (tests/test_launch_person_keys.py).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote

from utils.email_utils import is_placeholder_email

KEY_EMAIL = "email"
KEY_PHONE = "phone"
KEY_LINKEDIN = "linkedin"
KEY_JOBDIVA = "jobdiva"

# A row holds a person for its job while it has an interview id, or while a
# launch path holds it ('sent' without an id yet is a write-back in progress).
HOLDING_STATUSES = ("queued", "sending", "processing", "sent")

_NON_DIGIT_RE = re.compile(r"\D")
_LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", re.IGNORECASE)
_LINKEDIN_URL_FIELDS = ("profile_url", "linkedin_url", "public_profile_url", "linkedin")


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _data(row: Mapping[str, Any]) -> Mapping[str, Any]:
    data = row.get("data")
    return data if isinstance(data, Mapping) else {}


def email_key(value: Any) -> str:
    e = _text(value).lower()
    return "" if is_placeholder_email(e) else e


def phone_key(value: Any) -> str:
    digits = _NON_DIGIT_RE.sub("", _text(value))
    if len(digits) < 10 or len(set(digits)) < 4:
        return ""
    return digits[-10:]


def linkedin_key(value: Any) -> str:
    m = _LINKEDIN_SLUG_RE.search(_text(value))
    if not m:
        return ""
    return unquote(m.group(1)).strip().lower()


def jobdiva_key(value: Any) -> str:
    v = _text(value)
    return v if v.isdigit() else ""


def person_keys(row: Mapping[str, Any]) -> List[str]:
    """The person keys of one sourced row (column fields and its ``data``)."""
    data = _data(row)
    keys: List[str] = []

    def add(kind: str, value: str) -> None:
        if value:
            key = f"{kind}:{value}"
            if key not in keys:
                keys.append(key)

    add(KEY_EMAIL, email_key(row.get("email")) or email_key(data.get("email")))
    add(KEY_PHONE, phone_key(row.get("phone")) or phone_key(data.get("phone")))
    urls = data.get("urls") if isinstance(data.get("urls"), Mapping) else {}
    for raw in (
        [row.get(f) for f in _LINKEDIN_URL_FIELDS]
        + [data.get(f) for f in _LINKEDIN_URL_FIELDS]
        + [urls.get("linkedin")]
    ):
        add(KEY_LINKEDIN, linkedin_key(raw))
    add(KEY_JOBDIVA, jobdiva_key(data.get("jobdiva_candidate_id") or row.get("jobdiva_candidate_id")))
    if _text(row.get("source")).lower().startswith("jobdiva"):
        add(KEY_JOBDIVA, jobdiva_key(row.get("candidate_id")))
    return keys


def key_kind(key: str) -> str:
    return key.split(":", 1)[0]


def holds_person(row: Mapping[str, Any], *, session_id: str = "") -> bool:
    """Does this sourced row hold its person for the job?

    It does when it has an interview id, or a launch path holds it
    (``HOLDING_STATUSES``) — except a row merely 'queued' by ``session_id``
    itself: the claiming session's own not-yet-posted siblings never block
    each other (the request order decides between them instead).
    """
    data = _data(row)
    if _text(row.get("engage_interview_id") or data.get("engage_interview_id")):
        return True
    status = _text(row.get("engage_status") or data.get("engage_status")).lower()
    if status not in HOLDING_STATUSES:
        return False
    owner = _text(row.get("engage_session_id") or data.get("engage_session_id"))
    if status == "queued" and session_id and owner == session_id:
        return False
    return True


def find_person_duplicates(
    requested: Sequence[Tuple[str, Iterable[str]]],
    holders: Iterable[Tuple[str, Iterable[str]]],
) -> Dict[str, Tuple[str, str]]:
    """Which requested ids are a person someone else already holds.

    ``requested``: ``(candidate_id, keys)`` in request order (one entry per
    sourced row; an id may repeat — its keys are unioned). ``holders``:
    ``(candidate_id, keys)`` of every row / audit interview holding a person
    for the job. Returns ``{candidate_id: (other_candidate_id, key_kind)}``
    for each requested id to drop:

      * any key shared with a holder that is a DIFFERENT candidate_id, or
      * any key shared with an EARLIER requested id (two copies of one person
        in one request: the first one goes). A dropped id's other keys are
        attributed to the person it matched, so the match is transitive
        within the request (a ~phone~ b ~email~ c drops b and c).

    A requested id that is itself a holder is not dropped here: the claim's
    own per-id rule already refuses it.
    """
    owner: Dict[str, str] = {}
    for cid, keys in holders:
        cid = _text(cid)
        if not cid:
            continue
        for k in keys:
            owner.setdefault(k, cid)
    merged: Dict[str, List[str]] = {}
    order: List[str] = []
    for cid, keys in requested:
        cid = _text(cid)
        if not cid:
            continue
        if cid not in merged:
            merged[cid] = []
            order.append(cid)
        for k in keys:
            if k not in merged[cid]:
                merged[cid].append(k)
    taken: Dict[str, str] = {}
    out: Dict[str, Tuple[str, str]] = {}
    for cid in order:
        hit: Optional[Tuple[str, str]] = None
        for k in merged[cid]:
            other = owner.get(k)
            if other and other != cid:
                hit = (other, key_kind(k))
                break
            other = taken.get(k)
            if other and other != cid:
                hit = (other, key_kind(k))
                break
        if hit:
            out[cid] = hit
            # The dropped copy IS the holder's person: its other contacts are
            # that person's too, so a later copy sharing only those is dropped.
            for k in merged[cid]:
                taken.setdefault(k, hit[0])
            continue
        for k in merged[cid]:
            taken.setdefault(k, cid)
    return out


def launched_person_keys(rows: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    """``{person key: candidate_id}`` for the rows that hold a person (any
    session) — the selection's ``launched_person`` set."""
    out: Dict[str, str] = {}
    for row in rows:
        if not holds_person(row):
            continue
        cid = _text(row.get("candidate_id"))
        for k in person_keys(row):
            out.setdefault(k, cid)
    return out


def row_id(row: Mapping[str, Any]) -> str:
    """The id selection keys a pool row by (``rules.candidate_row_id``)."""
    for f in ("candidate_id", "jobdiva_candidate_id", "id"):
        v = _text(row.get(f))
        if v:
            return v
    return ""


LAUNCHED_PERSON = "launched_person"


def launched_person_blocks(pool: Iterable[Mapping[str, Any]], owners: Mapping[str, str]) -> Dict[str, str]:
    """``{row id: "launched_person"}`` for pool rows whose person is already
    held for the job by ANOTHER candidate_id (``owners`` from
    ``launched_person_keys``). Fed to the selection as ``blocked`` so the
    launch cap is never spent on a row the claim would refuse; the browser
    does the same (apps/web/lib/launch-person-keys.ts)."""
    out: Dict[str, str] = {}
    if not owners:
        return out
    for row in pool:
        cid = row_id(row)
        if not cid or cid in out:
            continue
        for k in person_keys(row):
            other = owners.get(k)
            if other and other != cid:
                out[cid] = LAUNCHED_PERSON
                break
    return out
