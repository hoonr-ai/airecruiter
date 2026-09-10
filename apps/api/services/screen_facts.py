"""Person-level facts learned from a PAIR screen, and their freshness rules.

The reuse boundary
------------------
A completed screen answers two different kinds of question:

* **Portable / logistics** — work authorization, visa sponsorship, acceptable
  work arrangements, expected pay, current location and role, availability.
  These describe the *person*, so they stay true across jobs and clients and are
  worth reusing instead of re-asking.
* **Job-specific** — "do you have hands-on experience with <this job's stack>",
  and interest in this particular role at this particular client. These are not
  transferable and are always asked fresh.

The codebase already draws exactly that line: `engagement.py` treats a question
as role-specific when `order_index > 9` or its category is neither `default` nor
`logistics`. `is_role_specific_question` below is the single shared home for that
rule, so the payload builder, the fact extractor and the re-check question
selector cannot drift apart.

Freshness
---------
Every fact carries a TTL (see `POOL_FACT_TTL_DAYS`). Work authorization barely
changes; "are you available to start soon" is stale in weeks. A fact past its TTL
is re-asked in the short L0.5 re-check rather than reused, so a downgraded screen
never rests on an answer that has expired.

Legacy contract
---------------
`eval_map_from_qa` reproduces the camelCase `eval_map` that
`routers/candidates.py` used to build inline and the candidate-details UI reads.
Both the legacy alias and the canonical `fact_key` come from one spec table here,
so adding a question in one place cannot silently miss the other.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.config import POOL_FACT_TTL_DAYS
from core.db import get_db_connection

logger = logging.getLogger(__name__)


# Default when a fact_key somehow has no configured TTL — short on purpose, so
# an unconfigured fact gets re-asked rather than trusted forever.
_DEFAULT_TTL_DAYS = 30


@dataclass(frozen=True)
class FactSpec:
    """One reusable fact and how to recognize the question that produces it."""

    fact_key: str
    # camelCase key the candidate-details UI already consumes.
    eval_alias: str
    # Human label used in the re-check question and in "why" chips.
    label: str
    # All substrings must be present (lowercased) for a question to match.
    must_include: Tuple[str, ...] = ()
    # Any one of these substrings matching is enough.
    any_of: Tuple[str, ...] = ()
    # A yes/no re-ask phrasing, used when the fact is stale and we need it again
    # inside a boolean (L0.5) re-check.
    recheck_question: str = ""

    def matches(self, question_text: str) -> bool:
        text = (question_text or "").lower()
        if not text:
            return False
        if self.must_include and not all(part in text for part in self.must_include):
            return False
        if self.any_of and not any(part in text for part in self.any_of):
            return False
        return bool(self.must_include or self.any_of)

    @property
    def ttl_days(self) -> int:
        return int(POOL_FACT_TTL_DAYS.get(self.fact_key, _DEFAULT_TTL_DAYS))


# Ordered: the first spec that matches wins, mirroring the if/elif chain this
# replaces.
#
# Each spec has to recognize THREE phrasings of the same question, because the
# same fact is asked differently depending on screening level:
#   1. the stored L1/L1.5/L2 wording  ("What is your current location?")
#   2. the L0.5 boolean rewrite from `engagement._to_boolean_question_text`
#      ("Are you currently based in the required job region for this role?")
#   3. the re-ask wording this module itself emits for a stale fact
#      ("Are you still based in the same location?")
# Matching only (1) — as the original inline chain did — meant an L0.5 job's
# screen produced zero reusable facts, which is precisely the cohort we most
# want to reuse. Any new `recheck_question` added below must classify back to
# its own fact_key; `tests/test_screen_facts.py` asserts that round trip.
FACT_SPECS: Tuple[FactSpec, ...] = (
    FactSpec(
        fact_key="work_authorization",
        eval_alias="isAuthorizedToWorkInUS",
        label="US work authorization",
        # Both parts required: "authorized to work in Canada" is a different
        # fact, and reusing it as US authorization would be a real mis-submit.
        must_include=("authorized to work", "united states"),
        recheck_question=(
            "Are you still authorized to work indefinitely for any employer "
            "in the United States?"
        ),
    ),
    FactSpec(
        fact_key="visa_sponsorship",
        eval_alias="requireVisaSponsorship",
        label="Visa sponsorship need",
        any_of=("visa sponsorship",),
        recheck_question=(
            "Will you now or in the future require visa sponsorship to continue "
            "working in the United States?"
        ),
    ),
    FactSpec(
        fact_key="work_arrangements",
        eval_alias="openWorkingArrangements",
        label="Acceptable work arrangements",
        # "working arrangements are you open to" = stored wording;
        # "these arrangements: w2, …" = the L0.5 boolean rewrite.
        any_of=("working arrangements", "these arrangements", "w2, subcontractor"),
        recheck_question=(
            "Are you still open to at least one of these arrangements: W2, "
            "Subcontractor, or Independent Contractor?"
        ),
    ),
    FactSpec(
        fact_key="relocation",
        eval_alias="isOpenToRelocation",
        label="Onsite / relocation",
        any_of=(
            "onsite work arrangement",
            "onsite arrangement",
            "open to relocation",
            "willing to relocate",
        ),
        recheck_question="Are you open to an onsite arrangement for this role?",
    ),
    FactSpec(
        fact_key="actively_looking",
        eval_alias="isActivelyLookingForJob",
        label="Actively looking",
        any_of=(
            "open to exploring",          # covers "…new job" and "…a new job"
            "actively looking",
            "open to new opportunities",
        ),
        recheck_question="Are you currently open to exploring a new job opportunity?",
    ),
    FactSpec(
        fact_key="current_role",
        eval_alias="currentRole",
        label="Current role",
        any_of=(
            "current or most recent role",
            "role similar to this position",   # L0.5 rewrite
            "same role you described",         # re-ask
        ),
        recheck_question="Are you still in the same role you described previously?",
    ),
    FactSpec(
        fact_key="current_location",
        eval_alias="currentLocation",
        label="Current location",
        any_of=(
            "current location",
            "based in the required job region",  # L0.5 rewrite
            "based in the same location",        # re-ask
        ),
        recheck_question="Are you still based in the same location?",
    ),
    FactSpec(
        fact_key="availability",
        eval_alias="isAvailableToStartSoon",
        label="Earliest availability",
        any_of=(
            "availability to start",
            "earliest availability",
            "available to start",   # L0.5 rewrite + re-ask
        ),
        recheck_question="Are you available to start within the required timeline for this role?",
    ),
    FactSpec(
        fact_key="expected_pay_rate",
        eval_alias="expectedPayRate",
        label="Compensation expectation",
        any_of=("compensation", "expected pay", "pay rate", "salary"),
        recheck_question="Are your compensation expectations aligned with this role?",
    ),
)

_SPEC_BY_KEY: Dict[str, FactSpec] = {s.fact_key: s for s in FACT_SPECS}

# Facts that describe the person and therefore travel between clients. Interest
# in a specific role is NOT in here and is never reused — see
# POOL_CROSS_CLIENT_REUSE_FACTS_ONLY.
PORTABLE_FACT_KEYS: Tuple[str, ...] = (
    "work_authorization",
    "visa_sponsorship",
    "work_arrangements",
    "current_location",
    "current_role",
    "expected_pay_rate",
)

# Facts that must be fresh before we will consider skipping outreach entirely
# (Tier C). These are the ones whose staleness would actually embarrass us in
# front of a client.
TIER_C_REQUIRED_FACT_KEYS: Tuple[str, ...] = (
    "work_authorization",
    "work_arrangements",
    "availability",
    "actively_looking",
)


def spec_for(fact_key: str) -> Optional[FactSpec]:
    return _SPEC_BY_KEY.get(fact_key)


def classify_question(question_text: str) -> Optional[str]:
    """Return the canonical `fact_key` a question produces, or None."""
    for spec in FACT_SPECS:
        if spec.matches(question_text):
            return spec.fact_key
    return None


def is_role_specific_question(
    category: Any, order_index: Any, *, default_when_missing: bool = False
) -> bool:
    """The shared "is this question job-specific?" rule.

    Mirrors `routers/engagement._sanitize_pre_screen_questions_for_pair`:
    anything past the fixed front-matter block (order_index > 9) or outside the
    `default`/`logistics` categories is role-specific. Compared
    case-insensitively because a stored category may be capitalized ("Default").
    """
    try:
        order = int(order_index or 0)
    except (TypeError, ValueError):
        order = 0
    cat = str(category or "").strip().lower()
    if not cat:
        return default_when_missing or order > 9
    return order > 9 or cat not in ("default", "logistics")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass
class ExtractedFact:
    fact_key: str
    fact_value: str
    normalized: Dict[str, Any] = field(default_factory=dict)


_YES_RE = re.compile(r"^\s*(yes|yeah|yep|correct|true|i am|i do|sure|absolutely)\b", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(no|nope|not really|negative|false|i am not|i do not|i don't)\b", re.IGNORECASE)


def _normalize_answer(fact_key: str, answer: str) -> Dict[str, Any]:
    """Best-effort structured form of a free-text answer.

    Only the boolean read is attempted. Anything richer (parsing a pay rate out
    of "around 75 an hour, maybe 80 for the right role") is deliberately left
    alone: a wrong structured value would be reused silently, whereas the raw
    text is always safe to show a recruiter.
    """
    out: Dict[str, Any] = {}
    text = (answer or "").strip()
    if not text:
        return out
    if _YES_RE.match(text):
        out["bool"] = True
    elif _NO_RE.match(text):
        out["bool"] = False
    return out


def facts_from_qa(
    qa_pairs: Iterable[Tuple[str, str]],
) -> List[ExtractedFact]:
    """Extract reusable facts from `(question, answer)` pairs.

    First answer per fact wins — PAIR asks each logistics question once, and if a
    transcript somehow repeats one, the earlier (unprompted) answer is the more
    trustworthy of the two.
    """
    seen: Dict[str, ExtractedFact] = {}
    for question, answer in qa_pairs:
        answer_text = str(answer or "").strip()
        if not answer_text:
            continue
        fact_key = classify_question(str(question or ""))
        if not fact_key or fact_key in seen:
            continue
        seen[fact_key] = ExtractedFact(
            fact_key=fact_key,
            fact_value=answer_text[:2000],
            normalized=_normalize_answer(fact_key, answer_text),
        )
    return list(seen.values())


def _qa_pairs_from_transcriptions(transcriptions: Any) -> List[Tuple[str, str]]:
    """Pull `(question, answer)` out of a webhook `transcriptions` list.

    Handles both the webhook's `question`/`answer` shape (see
    `routers/voice_agent.TranscriptionItem`) and the PAIR dashboard's
    `question_text`/`answer_text` shape, since both reach this code — the former
    from the live webhook, the latter from `data.engage_last_response`.
    """
    pairs: List[Tuple[str, str]] = []
    if not isinstance(transcriptions, list):
        return pairs
    for item in transcriptions:
        if not isinstance(item, dict):
            continue
        question = item.get("question") or item.get("question_text") or ""
        answer = item.get("answer") or item.get("answer_text") or ""
        if question:
            pairs.append((str(question), str(answer)))
    return pairs


def facts_from_webhook_payload(detail_payload: Any) -> List[ExtractedFact]:
    """Extract facts from the payload stored as `data.engage_last_response`.

    That blob is written from two directions — the live webhook builds
    `{"interview": …, "transcriptions": […]}` while the PAIR dashboard proxy
    nests the same content under `data` — so check both shapes plus the
    `questions_answers` spelling before giving up.
    """
    if not isinstance(detail_payload, dict):
        return []

    nested = detail_payload.get("data")
    nested = nested if isinstance(nested, dict) else {}

    for source in (
        detail_payload.get("transcriptions"),
        detail_payload.get("conversation"),
        nested.get("transcriptions"),
        nested.get("conversation"),
        detail_payload.get("questions_answers"),
        nested.get("questions_answers"),
    ):
        pairs = _qa_pairs_from_transcriptions(source)
        if pairs:
            return facts_from_qa(pairs)
    return []


def eval_map_from_qa(qa_list: Any) -> Dict[str, str]:
    """Legacy camelCase `eval_map` for the candidate-details response.

    Replaces the inline if/elif chain that used to live in
    `routers/candidates.get_candidate_report`. Same output keys, one spec table.
    """
    out: Dict[str, str] = {}
    if not isinstance(qa_list, list):
        return out
    for qa in qa_list:
        if not isinstance(qa, dict):
            continue
        question = qa.get("question_text") or qa.get("question") or ""
        answer = qa.get("answer_text") or qa.get("answer") or ""
        fact_key = classify_question(str(question))
        if not fact_key:
            continue
        spec = _SPEC_BY_KEY[fact_key]
        out.setdefault(spec.eval_alias, str(answer or ""))
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _expiry_for(fact_key: str, captured_at: datetime) -> datetime:
    spec = _SPEC_BY_KEY.get(fact_key)
    ttl = spec.ttl_days if spec else _DEFAULT_TTL_DAYS
    return captured_at + timedelta(days=ttl)


def write_facts(
    person_id: int,
    facts: Sequence[ExtractedFact],
    *,
    interview_id: Optional[str] = None,
    jobdiva_id: Optional[str] = None,
    captured_at: Optional[datetime] = None,
    source: str = "pair_screen",
    cur=None,
) -> int:
    """Upsert facts for a person. Returns the number written.

    A newer capture always wins (`captured_at >= existing`), so replaying an old
    webhook cannot roll a fact backwards.
    """
    if not person_id or not facts:
        return 0
    stamp = captured_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    def _run(active_cur) -> int:
        written = 0
        for fact in facts:
            import json

            active_cur.execute(
                """
                INSERT INTO candidate_screen_fact
                    (person_id, fact_key, fact_value, normalized, source,
                     interview_id, jobdiva_id, captured_at, expires_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                ON CONFLICT (person_id, fact_key) DO UPDATE SET
                    fact_value   = EXCLUDED.fact_value,
                    normalized   = EXCLUDED.normalized,
                    source       = EXCLUDED.source,
                    interview_id = EXCLUDED.interview_id,
                    jobdiva_id   = EXCLUDED.jobdiva_id,
                    captured_at  = EXCLUDED.captured_at,
                    expires_at   = EXCLUDED.expires_at
                WHERE EXCLUDED.captured_at >= candidate_screen_fact.captured_at
                """,
                (
                    person_id,
                    fact.fact_key,
                    fact.fact_value,
                    json.dumps(fact.normalized or {}),
                    source,
                    interview_id or None,
                    jobdiva_id or None,
                    stamp,
                    _expiry_for(fact.fact_key, stamp),
                ),
            )
            written += 1
        return written

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            written = _run(own_cur)
        conn.commit()
        return written
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("write_facts failed for person %s: %s", person_id, e)
        return 0
    finally:
        conn.close()


@dataclass
class StoredFact:
    fact_key: str
    fact_value: Optional[str]
    normalized: Dict[str, Any]
    captured_at: datetime
    expires_at: Optional[datetime]
    jobdiva_id: Optional[str]

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) < self.expires_at


def load_facts_bulk(person_ids: Sequence[int], *, cur=None) -> Dict[int, Dict[str, StoredFact]]:
    """`{person_id: {fact_key: StoredFact}}` for a batch of persons."""
    ids = [int(p) for p in person_ids if p]
    if not ids:
        return {}

    def _run(active_cur) -> Dict[int, Dict[str, StoredFact]]:
        active_cur.execute(
            """
            SELECT person_id, fact_key, fact_value, normalized, captured_at,
                   expires_at, jobdiva_id
            FROM candidate_screen_fact
            WHERE person_id = ANY(%s)
            """,
            (ids,),
        )
        out: Dict[int, Dict[str, StoredFact]] = {}
        for row in active_cur.fetchall() or []:
            out.setdefault(int(row[0]), {})[row[1]] = StoredFact(
                fact_key=row[1],
                fact_value=row[2],
                normalized=row[3] if isinstance(row[3], dict) else {},
                captured_at=row[4],
                expires_at=row[5],
                jobdiva_id=row[6],
            )
        return out

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            return _run(own_cur)
    except Exception as e:  # noqa: BLE001
        logger.warning("load_facts_bulk failed: %s", e)
        return {}
    finally:
        conn.close()


def stale_or_missing(
    facts: Dict[str, StoredFact],
    required_keys: Sequence[str],
    *,
    now: Optional[datetime] = None,
) -> List[str]:
    """Which of `required_keys` we cannot reuse — absent, or past their TTL."""
    moment = now or datetime.now(timezone.utc)
    return [k for k in required_keys if not (facts.get(k) and facts[k].is_fresh(moment))]


def facts_fresh_until(facts: Dict[str, StoredFact]) -> Optional[datetime]:
    """Earliest expiry across the portable facts we hold.

    This is the moment the person's reusable profile starts going stale, and is
    what `candidate_pool_state.facts_fresh_until` stores so the Candidates page
    can show a freshness chip without loading every fact.
    """
    expiries = [
        facts[k].expires_at
        for k in PORTABLE_FACT_KEYS
        if facts.get(k) and facts[k].expires_at is not None
    ]
    return min(expiries) if expiries else None


def recheck_questions_for(
    stale_keys: Sequence[str],
) -> List[Dict[str, Any]]:
    """Boolean re-ask questions for the facts we could not reuse.

    Shaped like the rows `_sanitize_pre_screen_questions_for_pair` expects:
    category `logistics` and `order_index` inside the front-matter block, so the
    existing L0.5 hard-filter handling applies to them unchanged.
    """
    questions: List[Dict[str, Any]] = []
    for idx, key in enumerate(stale_keys):
        spec = _SPEC_BY_KEY.get(key)
        if not spec or not spec.recheck_question:
            continue
        questions.append({
            "question_text": spec.recheck_question,
            "pass_criteria": f"Pass when the candidate confirms {spec.label.lower()}.",
            "is_default": True,
            "category": "logistics",
            "order_index": idx + 2,  # leaves order_index 1 for the interest question
            "is_hard_filter": True,
        })
    return questions
