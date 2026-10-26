"""Shared constants, criteria model and module-level helpers for unified candidate search."""
import logging
import contextvars
import re
from typing import List, Dict, Any, Optional, Sequence, Tuple
from core import pipeline_trace as _pipeline_trace
from pydantic import BaseModel, Field, model_validator
from services.location import haversine_miles, region_core_city
from core.config import SCORING_BAND_EXCELLENT, SCORING_BAND_STRONG, SCORING_BAND_GOOD
from services import role_taxonomy


_TITLE_BOOST_BY_RELEVANCE = {"exact": 30, "similar": 20, "related": 10}

# First explainability line under the scoring matrix, keyed by band tier
# (core.config.score_band). The high-level JobAgent branch in
# finalize_candidate strips these (it withholds the % they describe).
_MATRIX_BAND_LINES = {
    "excellent": f"Excellent match ({SCORING_BAND_EXCELLENT}–100) — priority outreach",
    "strong": f"Strong match ({SCORING_BAND_STRONG}–{SCORING_BAND_EXCELLENT - 1}) — recommended",
    "good": f"Good match ({SCORING_BAND_GOOD}–{SCORING_BAND_STRONG - 1}) — recruiter review",
    "low": f"Low priority (<{SCORING_BAND_GOOD}) — below the outreach floor",
    "unscored": "Unscored — limited data",
}


_TITLE_SEPARATORS = re.compile(r"[/|·,;]+")


def _candidate_titles(cand: Dict[str, Any]) -> List[str]:
    """Pull every plausible title field off a candidate dict, split on common
    separators ("/", "|", "·", ",", ";") so multi-title strings like
    "BI Developer/SQL Server Developer/Power BI" become three lookups, deduped."""
    raw: List[str] = []
    for key in ("title", "current_title", "most_recent_title", "headline"):
        val = cand.get(key)
        if isinstance(val, str) and val.strip():
            raw.append(val.strip())
    enhanced = cand.get("enhanced_info")
    if isinstance(enhanced, dict):
        for key in ("current_title", "most_recent_title", "headline"):
            val = enhanced.get(key)
            if isinstance(val, str) and val.strip():
                raw.append(val.strip())
    out: List[str] = []
    for combined in raw:
        for part in _TITLE_SEPARATORS.split(combined):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _compute_title_boost(cand: Dict[str, Any], title_criteria: List[Dict[str, Any]]) -> int:
    """Score how well a candidate's title matches the searched titles via taxonomy.

    Returns the strongest tier across (searched_title × candidate_title): exact=30,
    similar=20, related=10, none=0. Each searched title contributes its primary
    `value` plus any `similar_terms` already attached upstream.
    """
    candidate_titles = _candidate_titles(cand)
    if not candidate_titles:
        return 0
    best = 0
    best_pair: Optional[Tuple[str, str, str]] = None
    for item in title_criteria:
        searched_values = [str(item.get("value", "")).strip()]
        for t in item.get("similar_terms", []) or []:
            t = str(t).strip()
            if t:
                searched_values.append(t)
        for searched in searched_values:
            if not searched:
                continue
            for cand_title in candidate_titles:
                tier = role_taxonomy.compare(searched, cand_title)
                points = _TITLE_BOOST_BY_RELEVANCE.get(tier, 0)
                if points > best:
                    best = points
                    best_pair = (searched, cand_title, tier)
                    if best >= 30:
                        cand["title_match_source"] = {"searched": searched, "candidate": cand_title, "relevance": tier}
                        return best
    if best > 0 and best_pair:
        cand["title_match_source"] = {"searched": best_pair[0], "candidate": best_pair[1], "relevance": best_pair[2]}
    return best


_TITLE_GATE_WORD_RE = re.compile(r"[^a-z0-9+#.]+")
# Connective words that carry no role meaning: "Director of Engineering" must
# match "Director, Engineering". Everything else — including short role words
# like "QA" / "BI" / "UX" — is significant, so "Tech Lead" does not satisfy
# "QA Lead" (Step 5's external-source check skipped <4-char tokens; this gate
# is deliberately the tighter reading of the same rule).
_TITLE_GATE_STOPWORDS = frozenset({"of", "and", "the", "for", "in", "to", "a", "an", "&", "or", "with", "at"})


def _title_gate_normalize(value: Any) -> str:
    """Lower-case, punctuation → space, collapsed whitespace ("Sr. Data-Analyst" → "sr data analyst")."""
    text = _TITLE_GATE_WORD_RE.sub(" ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip(" .")


def title_relevance_gate(
    candidate: Dict[str, Any], title_criteria: List[Dict[str, Any]]
) -> Tuple[bool, str]:
    """Would this profile have entered the Step-5 pool for these title chips?

    Step 5 never scores a candidate whose *title* is off-role: the JobDiva
    TalentSearch boolean carries the role chips in the dedicated
    ``TITLES= (...)`` field (matched against the candidate's job title, not
    the résumé body — see ``_build_boolean_string``), and every external
    source runs ``_candidate_title_match``. The scoring matrix then only
    *ranks* what got in — title relevance is 15 of 100 points, so an
    off-role profile with the right skills still scores well. Any caller
    that scores a pool Step 5 did not source (cross submissions re-scoring
    previously screened people) must apply this gate first, or a "QA Lead"
    surfaces as a 77% match for a Data Analyst req.

    A candidate title matches a role term when (any of):
      * the role phrase appears in the title ("data analyst" in "senior data analyst"),
      * every role word (connectives aside — ``_TITLE_GATE_STOPWORDS``) appears
        in the title (the ``_candidate_title_match`` token rule, applied to
        title fields only — never the résumé — and to short words like "QA"),
      * the role taxonomy puts both in the same tight family
        (``role_taxonomy.compare`` → exact/similar — the tiers the matrix's
        title bucket credits).

    Role terms = every included title chip's value plus its recruiter-approved
    ``similar_terms`` (the same variants the boolean builder ORs together).
    No title chips → nothing to gate on → pass. Title chips but no title on
    the profile → fail (that is what ``TITLES=`` does), reason ``no_title_on_profile``.

    Returns ``(passed, reason)``; on a pass the reason names the winning
    ``role ~ candidate title`` pair for logs / explainability.
    """
    role_terms: List[str] = []
    seen_roles = set()
    for item in title_criteria or []:
        if not isinstance(item, dict) or _is_excluded_criterion(item):
            continue
        for raw in [item.get("value")] + list(item.get("similar_terms") or []):
            term = _title_gate_normalize(raw)
            if term and term not in seen_roles:
                seen_roles.add(term)
                role_terms.append(term)
    if not role_terms:
        return True, "no_title_criteria"

    raw_titles = list(_candidate_titles(candidate))
    enhanced = candidate.get("enhanced_info")
    if isinstance(enhanced, dict):
        # Step 5 stamps ``title = enhanced_info.job_title`` after enrichment;
        # a stored row may only carry it here.
        for key in ("job_title", "title"):
            val = enhanced.get(key)
            if isinstance(val, str) and val.strip():
                raw_titles.extend(p.strip() for p in _TITLE_SEPARATORS.split(val) if p.strip())
    cand_titles: List[str] = []
    seen_titles = set()
    for raw in raw_titles:
        norm = _title_gate_normalize(raw)
        if norm and norm not in seen_titles:
            seen_titles.add(norm)
            cand_titles.append(norm)
    if not cand_titles:
        return False, "no_title_on_profile"

    for role in role_terms:
        role_tokens = [t for t in role.split() if t not in _TITLE_GATE_STOPWORDS]
        for title in cand_titles:
            if role in title:
                return True, f"{role} ~ {title}"
            title_tokens = set(title.split()) - _TITLE_GATE_STOPWORDS
            if role_tokens and all(t in title_tokens for t in role_tokens):
                return True, f"{role} ~ {title}"
            try:
                if role_taxonomy.compare(role, title) in ("exact", "similar"):
                    return True, f"{role} ~ {title}"
            except Exception:  # noqa: BLE001 — taxonomy is an assist, never a failure
                pass
    return False, "title_mismatch"

logger = logging.getLogger(__name__)

# Sentinel distance for candidates with no usable location data. Big enough
# to always exceed the UI's within_miles radius so the BEYOND 25MI badge
# counts these rows instead of silently passing them as in-radius.
_UNKNOWN_DISTANCE_SENTINEL = 9999.0


def _broad_core_radius_distance(
    broad_locs, required: Dict[str, str], criteria: "SearchCriteria"
) -> Optional[float]:
    """The closest known metro-core-to-search-point distance among
    ``broad_locs``, or None if none of them has a resolvable core at all.

    The caller tells "core is known and far" (returns a real mileage, used
    to hard-drop — e.g. "Dallas-Fort Worth Metroplex" is ~225mi from a
    Houston search, same state, clearly a different metro) apart from "no
    core known at all" (returns None, soft-kept — an unrecognized label
    carries no distance information either way) by checking the return
    value, not a separate flag: a plain bool here previously conflated
    "confirmed far" with "unknown," which is the bug this type exists to
    prevent.

    Offline ZIP points only. The broad label is not sent to a geocoder.
    """
    from services import zip_index

    required_point = None
    if required.get("zip"):
        required_point = zip_index.zip_centroid(required["zip"])
    if required_point is None and required.get("city") and required.get("state"):
        required_point = zip_index.city_state_representative_point(
            required["city"], required["state"]
        )
    if required_point is None:
        return None
    closest: Optional[float] = None
    for loc in broad_locs:
        core = region_core_city(loc)
        if not core:
            continue
        point = zip_index.city_state_representative_point(core[0], core[1])
        if point is None:
            continue
        distance = haversine_miles(
            point[0], point[1], required_point[0], required_point[1]
        )
        if closest is None or distance < closest:
            closest = distance
    return closest


def _richer_profile_location(current: str, recovered: str) -> str:
    """Return the profile location when it is new, including a wrong city.

    Pass A can store a blank line, a bare city, or a city taken from a job
    description. The later profile read replaces any of those. An identical
    string is left alone.
    """
    current_text = str(current or "").strip()
    recovered_text = str(recovered or "").strip()
    if not recovered_text:
        return ""
    if recovered_text.casefold() == current_text.casefold():
        return ""
    return recovered_text


def _is_excluded_criterion(item: Dict[str, Any]) -> bool:
    """Whether a rubric chip is an EXCLUDE, tolerant of spelling/casing.

    The wizard emits lowercase "exclude", but other writers produce "must_not"
    and capitalised forms, and an exact `== "exclude"` compare silently treats
    those as INCLUDE — which is worse than ignoring them: the term becomes
    something we actively search FOR. Mirrors the normalisation used by
    sourcing_skills_with_priority and the boolean builder.
    """
    match_type = str(item.get("match_type", "must") or "must").lower()
    return match_type.replace("_", " ").strip() in {"exclude", "must not"}


def resolve_jobdiva_sources(sources: Sequence[str]) -> Dict[str, bool]:
    """Which JobDiva producers a request's `sources` list selects.

    JobDiva is three independent producers, each with its own Step-5
    checkbox (or, for Applicants, its own auto-sync path):

      - Applicants:   people who applied to this job_id (no boolean)
      - JobAgent:     JobDiva's AI matcher, driven by the criteria the
                      recruiter configured on the req inside JobDiva
      - TalentSearch: the boolean query PAIR generates

    Bare ``"JobDiva"`` is the legacy combined value — older saved drafts
    and non-wizard callers still send it, and it keeps meaning "run both
    talent pools". Separating them lets a recruiter spend the search
    budget on whichever pool is actually productive for the req.
    """
    selected = set(sources or ())
    legacy_both = "JobDiva" in selected
    return {
        "applicants": bool(
            {"JobDiva Applicants", "JobDiva-Applicants"} & selected
        ),
        "jobagent": legacy_both or "JobDiva-JobAgent" in selected,
        "talentsearch": legacy_both or "JobDiva-TalentSearch" in selected,
    }


# Role family for scoring OUTSIDE a live search (`score_candidate_off_search`).
# `_current_family` is instance state set once per `search_candidates` call on a
# module-level singleton, so an off-search scorer (the cross-submissions scan,
# the re-score endpoints) would otherwise read whichever family the last search
# happened to leave behind — and clobber it for a search still streaming.
# A ContextVar is per-task/per-thread (`asyncio.to_thread` copies the context),
# so each off-search scorer gets its own value and the live search keeps its own.
_UNSET_FAMILY = "\x00unset"
_off_search_family: contextvars.ContextVar = contextvars.ContextVar(
    "scoring_role_family", default=_UNSET_FAMILY
)


class LocationEntry(BaseModel):
    """Typed entry for an additional sourcing location."""
    value: str
    within_miles: int = 25


# Maximum number of additional locations accepted per request to cap
# provider fan-out and protect latency and quota.
_MAX_LOCATIONS: int = 10
# Unipile is paused for new sourcing searches. Keep its implementation and
# profile-enrichment path available for focused tests and a future re-enable.
LINKEDIN_UNIPILE_SEARCH_ENABLED = False
DISABLED_EXTERNAL_SOURCES = frozenset({"LinkedIn"})


class SearchCriteria(BaseModel):
    job_id: str
    title_criteria: List[Dict[str, Any]] = []
    skill_criteria: List[Dict[str, Any]] = []
    keywords: List[str] = []
    resume_match_filters: List[Dict[str, Any]] = []
    location: str = ""
    within_miles: int = 25
    # Multi-location sourcing: additional locations beyond the primary.
    # Each entry has "value" (location string) and optionally "within_miles".
    # The location filter passes if the candidate is near ANY location.
    # Capped at _MAX_LOCATIONS entries to prevent unbounded provider fan-out.
    additional_locations: List[LocationEntry] = []
    # Job work arrangement ("Remote" | "Hybrid" | "Onsite" | "Unspecified").
    # Remote jobs skip the commute-radius constraint entirely (any US
    # location passes; non-US is still dropped).
    location_type: str = "Unspecified"
    # Structured geo for JobDiva talentSearchDef. Optional — backend will
    # derive these from `location` when the frontend doesn't send them.
    countries: List[str] = []
    states: List[str] = []
    page_number: int = 0
    companies: List[str] = []
    page_size: int = 100
    sources: List[str] = ["JobDiva", "LinkedIn", "Exa"]
    open_to_work: bool = True
    boolean_string: str = ""
    bypass_screening: bool = False
    # 5.6: limit JobDiva Talent Search to candidates whose record was touched in
    # the last N days. None / 0 means "no freshness filter".
    recent_days: Optional[int] = None
    # 5.10: by default JobDiva Talent Search drops profile-only candidates
    # (no resume attached). Set false to opt back in to the full signal.
    require_resume: bool = True
    include_relocation_candidates: bool = True
    min_experience_years: Optional[int] = None
    max_experience_years: Optional[int] = Field(None, ge=1, le=40)

    @model_validator(mode="after")
    def experience_years_form_a_valid_range(self):
        """Keep saved/re-score criteria from creating an impossible YOE range."""
        minimum = self.min_experience_years
        maximum = self.max_experience_years
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("min_experience_years cannot exceed max_experience_years")
        return self
    # JobDiva tranche controls. Initial search uses offset=0/batch=150; the
    # "Search more" action uses offset=150/batch=150 to append the next page.
    jobdiva_offset: int = 0
    jobdiva_batch_size: int = 150
    # Hiring client / account name. Drives two rubric signals: the
    # "currently employed by client" hard gate (via current-employer
    # exclusion) and the positive "Same client / industry experience"
    # dimension. Optional — when blank both signals degrade gracefully
    # (the same-client dimension drops out and redistributes).
    client_name: str = ""
    # Sample-first flow. "sample": each selected source probes a small ranked
    # pool (SAMPLE_MODE_POOL_SIZE), fully enriches + scores it, and emits only
    # the first `sample_per_source` rows that clear the normal gates — cheap
    # source-quality preview before the recruiter commits to the full run.
    # "full": normal behavior.
    search_mode: str = "full"
    sample_per_source: int = 2
    # Run the full LLM skills-match on every source, including JobDiva-JobAgent
    # rows (which otherwise use high-level scoring and render score-less).
    # Used by the auto-launch flow so every row carries a comparable numeric
    # match_score (auto-launch takes every launchable candidate; scores only
    # order its safety cap).
    assess_all_sources: bool = False
    # The search is about to be launched (Source & Launch's full run and its
    # relaxed runs, a launch session's S1): LinkedIn rows' paid Exa email
    # lookup is put off to Launch PAIR, which asks for the email and the
    # phone in one run (_paid_contact_deferred; kill switch
    # SOURCING_DEFER_PAID_CONTACT_ENABLED). Apollo still runs.
    defer_paid_contact: bool = False

    def sourcing_skill_values(self) -> List[str]:
        """Flat skill-like strings for sources that only accept a plain list
        (LinkedIn-Unipile, Exa, Dice, Vetted). Pulls from skill_criteria +
        title_criteria, skipping excludes and empty values."""
        values: List[str] = []
        seen = set()
        for item in (self.skill_criteria or []) + (self.title_criteria or []):
            if not isinstance(item, dict):
                continue
            if item.get("match_type") == "exclude":
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
        return values

    def sourcing_skills_with_priority(self) -> List[Dict[str, str]]:
        """Like sourcing_skill_values, but each term keeps a coarse priority
        ('Must Have' | 'Preferred') derived from its rubric match_type.

        Lets the Unipile layer map required-vs-optional onto LinkedIn
        MUST_HAVE / CAN_HAVE instead of ANDing every term together — sending
        everything as MUST_HAVE collapsed searches to ~1 result. Skills come
        before titles so the downstream MUST_HAVE cap fills from the more
        reliably-resolvable skill terms first. Excludes/empties dropped;
        deduped by lowercased value (first occurrence wins)."""
        out: List[Dict[str, str]] = []
        seen = set()
        for item in (self.skill_criteria or []) + (self.title_criteria or []):
            if not isinstance(item, dict):
                continue
            match_type = str(item.get("match_type", "must") or "must").lower().replace("_", " ").strip()
            if match_type in {"exclude", "must not"}:
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            priority = "Preferred" if match_type in {"can", "preferred", "nice to have"} else "Must Have"
            out.append({"value": value, "priority": priority})
        return out

    def skill_only_values(self) -> List[str]:
        """Plain skill strings from skill_criteria ONLY (titles excluded).

        Used for the natural-language Exa/Dice queries where titles and
        skills occupy different slots of the sentence — mixing titles into
        the skill list (as sourcing_skill_values does) reads as
        '<title> with <title>, <skill> experience'."""
        values: List[str] = []
        seen = set()
        for item in self.skill_criteria or []:
            if not isinstance(item, dict):
                continue
            if item.get("match_type") == "exclude":
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
        return values

    def sourcing_titles(self) -> List[str]:
        """Plain non-exclude title strings, in criteria order, deduped.

        Feeds the one-role-per-search Exa/Dice fan-out and the Exa Agent
        deep-search prompt."""
        titles: List[str] = []
        seen = set()
        for item in self.title_criteria or []:
            if not isinstance(item, dict):
                continue
            if _is_excluded_criterion(item):
                continue
            value = str(item.get("value", "")).strip()
            key = value.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            titles.append(value)
        return titles

    def title_variants(self, max_titles: int = 3) -> List[str]:
        """Role variants for multi-pull title searches, most important first.

        Order: primary title chip, then ITS selected similar titles
        (`similar_terms`, the recruiter-approved grounded variants), then the
        next title chip and its variants. Deduped case-insensitively and
        capped at `max_titles` so the per-variant TalentSearch pulls stay
        bounded."""
        variants: List[str] = []
        seen = set()

        def _add(value: Any) -> None:
            v = str(value or "").strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                variants.append(v)

        for item in self.title_criteria or []:
            if not isinstance(item, dict):
                continue
            if _is_excluded_criterion(item):
                continue
            _add(item.get("value"))
            for similar in item.get("similar_terms") or []:
                _add(similar)
        return variants[: max(1, int(max_titles or 1))]


def _below_contact_score_floor(cand: Dict[str, Any]) -> bool:
    """True when ``cand['match_score']`` is a number below CONTACT_ENRICH_MIN_SCORE."""
    from core import sourcing_config as _sc_floor
    try:
        score = float(cand.get("match_score"))
    except (TypeError, ValueError):
        return False
    return score < float(getattr(_sc_floor, "CONTACT_ENRICH_MIN_SCORE", 60))


def _paid_contact_deferred(cand: Dict[str, Any], criteria: Any) -> bool:
    """Whether this LinkedIn row's paid (Exa) email lookup is put off to
    Launch PAIR: the search is about to be launched (criteria
    ``defer_paid_contact``: Source & Launch's full run, a launch session's
    S1) and SOURCING_DEFER_PAID_CONTACT_ENABLED. Launch asks the normal run
    for the email and the phone at once instead of buying a second run for
    the phone minutes later, and nobody unlaunched is paid for. getattr
    throughout: criteria built without the field (tests, older callers)
    still enrich."""
    from core import sourcing_config as _sc_defer
    return (
        bool(getattr(criteria, "defer_paid_contact", False))
        and bool(getattr(_sc_defer, "SOURCING_DEFER_PAID_CONTACT_ENABLED", True))
        and str(cand.get("source") or "").startswith("LinkedIn")
    )


# Job id of the search running in this task context; hydration reads it to
# know which jd:active marker is its own.
_search_job_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ucs_search_job_id", default=None
)


async def _traced(stage: str, coro, **attrs):
    """Await ``coro`` inside a pipeline_trace span (timing only)."""
    async with _pipeline_trace.span(stage, **attrs):
        return await coro
