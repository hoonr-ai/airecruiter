"""UnifiedCandidateSearch methods: search terms."""
import json
import re
from typing import List, Dict, Any
from .common import SearchCriteria, logger


class SearchTermsMixin:

    @staticmethod
    def _candidate_haystack(c: Dict[str, Any]) -> str:
        """Concatenated lowercase text used for skill keyword matching."""
        parts = [
            str(c.get("title") or ""),
            str(c.get("abstract") or ""),
            str(c.get("resume_text") or ""),
            " ".join(str(s) for s in (c.get("skills") or [])),
        ]
        return " ".join(p for p in parts if p).lower()

    def _rank_candidates_by_skill(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
        keep_top: int,
    ) -> List[Dict[str, Any]]:
        """Pre-rank candidates by skill keyword match against title/abstract/resume.

        Used on the TalentSearch fallback path where JobDiva returns its broken
        fixed pool — without this, the downstream LLM ranker sees ~280 mostly-
        irrelevant candidates and surfaces poor matches. Scoring:

        - +3 per `must` skill or title term hit (case-insensitive substring).
        - +1 per `can` (preferred) hit.
        - +1 per plain `keywords[]` hit.
        - -10 per `exclude` term hit (effectively drops the candidate).
        - Drop candidates whose `must` hit-count is 0 when `must` terms exist.

        Returns at most `keep_top` candidates, sorted by score desc, ties
        broken by JobDiva's original ordering.
        """
        must_terms: List[str] = []
        can_terms: List[str] = []
        exclude_terms: List[str] = []
        for item in (criteria.title_criteria or []) + (criteria.skill_criteria or []):
            value = str(item.get("value", "")).strip().lower()
            if not value:
                continue
            mt = item.get("match_type", "must")
            if mt == "exclude":
                exclude_terms.append(value)
            elif mt == "can":
                can_terms.append(value)
            else:
                must_terms.append(value)
        keyword_terms = [str(k).strip().lower() for k in (criteria.keywords or []) if str(k).strip()]

        # If we have no skill signal at all, leave the order alone.
        if not (must_terms or can_terms or keyword_terms or exclude_terms):
            return candidates[:keep_top]

        # Match a term against haystack: full-phrase match counts 1.0;
        # otherwise count significant tokens (len>=4) and award a partial
        # match scaled to coverage. This matters because users type long
        # phrases ("Java Full Stack Development") but resumes have shorter
        # canonical forms ("Java"). Without partial match we would over-drop.
        def term_score(term: str, hay_text: str) -> float:
            if not term or not hay_text:
                return 0.0
            if term in hay_text:
                return 1.0
            tokens = [t for t in term.split() if len(t) >= 4]
            if not tokens:
                return 0.0
            hits = sum(1 for t in tokens if t in hay_text)
            cov = hits / len(tokens)
            # Require at least one significant token; reward higher coverage.
            return cov if cov >= 1.0 / max(1, len(tokens)) else 0.0

        # First pass — strict scoring. Drop candidates that miss every must
        # term or trigger any exclude term, score the rest.
        strict: List[tuple] = []
        soft: List[tuple] = []  # parallel weak-signal pool used only if strict is empty
        any_haystack = 0
        for idx, c in enumerate(candidates):
            hay = self._candidate_haystack(c)
            if hay:
                any_haystack += 1
            must_score = sum(term_score(t, hay) for t in must_terms) if hay else 0
            must_hits = 1 if must_score > 0 else 0
            can_score = sum(term_score(t, hay) for t in can_terms) if hay else 0
            kw_score = sum(term_score(t, hay) for t in keyword_terms) if hay else 0
            exc_hits = sum(1 for t in exclude_terms if t in hay) if hay else 0
            soft_score = can_score + kw_score - 10.0 * exc_hits
            strict_score = 3.0 * must_score + soft_score
            if exc_hits:
                continue  # always drop exclude-hits regardless of mode
            if must_terms:
                if must_hits > 0 and strict_score > 0:
                    strict.append((strict_score, idx, c))
                else:
                    soft.append((soft_score, idx, c))
            else:
                strict.append((strict_score, idx, c))

        # Soft-mode fallback: when haystacks are mostly empty (TalentSearch
        # often returns thin records) or no strict matches survive, fall back
        # to "any signal beats nothing" so we don't return 0.
        sparse = any_haystack < max(3, len(candidates) // 3)
        if not strict or sparse:
            if sparse:
                logger.debug(
                    "_rank_candidates_by_skill: sparse haystacks "
                    f"({any_haystack}/{len(candidates)}); using soft mode"
                )
            else:
                logger.debug(
                    "_rank_candidates_by_skill: no strict matches; using soft mode"
                )
            scored = strict + soft
            # Original-order tiebreak preserves JobDiva's ranking when scores tie.
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [c for _, _, c in scored[:keep_top]]

        strict.sort(key=lambda t: (-t[0], t[1]))
        return [c for _, _, c in strict[:keep_top]]

    @staticmethod
    def _strip_location_from_boolean(boolean: str, location: str) -> str:
        """Remove the auto-appended `"<location>"` term from a boolean string.

        Conservative: only strips an exact quoted match (case-insensitive)
        with adjacent ` AND ` glue. If the location can't be found cleanly,
        returns the input unchanged so user-typed booleans aren't mangled.
        """
        if not boolean:
            return boolean
        loc = (location or "").strip()
        if not loc:
            return boolean

        quoted = re.escape(f'"{loc}"')
        patterns = [
            rf'\s+AND\s+{quoted}(?=\s|$|\))',  # mid/tail: " AND \"X\""
            rf'(?<=^){quoted}\s+AND\s+',       # leading: "\"X\" AND "
            rf'(?<=\()\s*{quoted}\s+AND\s+',   # inside group: "(\"X\" AND ..."
            rf'\s+AND\s+{quoted}(?=\))',       # before group close
        ]
        out = boolean
        for pat in patterns:
            new_out = re.sub(pat, lambda m: ' ' if m.group(0).startswith(' AND ') else '', out, flags=re.IGNORECASE)
            if new_out != out:
                out = new_out
                break
        return re.sub(r'\s+', ' ', out).strip()

    def _jobdiva_search_terms(self, criteria: SearchCriteria) -> List[Dict[str, Any]]:
        terms: List[Dict[str, Any]] = []
        for item in criteria.title_criteria + criteria.skill_criteria:
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            terms.append({
                "value": value,
                "match_type": item.get("match_type", "must"),
                "years": item.get("years", 0),
                "recent": item.get("recent", False),
            })
        for value in criteria.keywords:
            if value and value.strip():
                terms.append({"value": value.strip(), "match_type": "must"})
        return terms

    def _build_boolean_string(
        self, criteria: SearchCriteria, dialect: str = "generic"
    ) -> str:
        """Build the sourcing boolean from the Step 5 filters.

        ``dialect="jobdiva"`` emits JobDiva's native shape — roles in the
        dedicated ``TITLES= (...)`` field and ``IN {US}`` for a remote role's
        geo, i.e. what a recruiter hand-writes into JobAgent criteria.
        ``"generic"`` (the default) keeps a plain boolean expression, because
        Unipile and Exa parse neither of those constructs.

        AND/OR *allocation* matters far more here than term count, because
        every AND multiplies the constraint while every OR widens it:

          - **Roles are alternatives** → one OR group, AND'ed in once. Nobody
            is a "Senior Data Engineer" and an "ETL Developer" at the same
            time, so ANDing title clauses matched ~nobody.
          - **Skills are requirements** → they AND, but only the most
            important `JOBDIVA_BOOLEAN_MUST_SKILL_CAP` of them. The ranked
            overflow is demoted into the preferred OR group instead of being
            dropped, so it still lifts ranking without gating the search.
          - **Companies are alternatives** ("worked at any of these") → OR.
          - **Excludes** → a single trailing `NOT (...)` group.

        Importance ranking for the capped skill ANDs matches the structured
        TalentSearch term selection (role-named skills first, then ones
        carrying explicit years/recency), so the boolean a recruiter reads and
        the query we actually run agree on what the core requirements are.
        """
        from services.jobdiva_boolean_translator import term_appears_as_token
        from services.rubric_grounding import is_industry_term as term_is_industry
        from core import sourcing_config as _sc_bool

        must_skill_cap = max(
            1, int(getattr(_sc_bool, "JOBDIVA_BOOLEAN_MUST_SKILL_CAP", 4) or 4)
        )
        # The role the skill ranking is measured against — the first included
        # title chip. With no title chips the ranking simply falls back to
        # years/recency then chip order.
        primary_role = ""
        for _t in criteria.title_criteria or []:
            _mt = str(_t.get("match_type", "must") or "must").lower().replace("_", " ").strip()
            if _mt in {"exclude", "must not"}:
                continue
            primary_role = str(_t.get("value", "")).strip()
            if primary_role:
                break

        def quote(value: str) -> str:
            return f'"{value.strip()}"'

        def normalize_term(value: str) -> str:
            value = str(value or "").lower().strip()
            value = value.replace('"', "").replace("(", "").replace(")", "")
            value = re.sub(r"^must be local to\s*", "", value)
            value = re.sub(r"\s*metro$", "", value)
            value = re.sub(r"^must not be employed by:\s*", "", value)
            value = re.sub(r"\s+within\s+\d+\s+mi$", "", value)
            value = re.sub(r"\s+recent$", "", value)
            value = re.sub(r"\s+over\s+\d+\s+years?$", "", value)
            return re.sub(r"\s+", " ", value).strip()

        def add_unique(bucket: List[str], seen: set, clause: str, key_value: str = "") -> None:
            key = normalize_term(key_value or clause)
            if not clause or not key or key in seen:
                return
            seen.add(key)
            bucket.append(clause)

        def match_type_of(item: Dict[str, Any]) -> str:
            """Normalized rubric match type.

            The wizard emits must/can/exclude, but casing and `must_not`
            spellings turn up from other writers. The old exact `== "exclude"`
            compare fell through to the else-branch, promoting an EXCLUDED term
            into the REQUIRED AND chain — the worst possible misread.
            """
            mt = str(item.get("match_type", "must") or "must").lower()
            mt = mt.replace("_", " ").strip()
            if mt in {"exclude", "must not"}:
                return "exclude"
            if mt in {"can", "preferred", "nice to have"}:
                return "can"
            return "must"

        def variants_of(item: Dict[str, Any]) -> List[str]:
            """Term + its recruiter-approved similar terms, registered for
            dedup and returned quoted."""
            out: List[str] = []
            value = str(item.get("value", "")).strip()
            if value:
                source_keys.add(normalize_term(value))
                out.append(value)
            for similar in item.get("similar_terms", []) or []:
                s = str(similar).strip()
                if s:
                    source_keys.add(normalize_term(s))
                    out.append(s)
            return out

        must_groups: List[str] = []
        can_terms: List[str] = []
        exclude_terms: List[str] = []
        seen_must = set()
        seen_can = set()
        seen_exclude = set()
        source_keys = set()

        # ── Roles: alternatives, so ONE OR group ─────────────────────────
        # A candidate is a "Senior Data Engineer" OR an "ETL Developer" —
        # essentially never both. ANDing title clauses together was the single
        # biggest recall killer in this builder: two ANDed titles already
        # matched ~nobody, and the old workaround (truncate to the first two)
        # both over-constrained AND discarded the remaining role variants.
        # Collapsing every included title plus its similar titles into one OR
        # group means extra variants now *widen* recall instead of destroying
        # it, so none have to be thrown away.
        role_terms: List[str] = []
        seen_roles = set()
        for item in criteria.title_criteria or []:
            mt = match_type_of(item)
            terms = variants_of(item)
            if not terms:
                continue
            if mt == "exclude":
                group = (
                    quote(terms[0]) if len(terms) == 1
                    else f"({' OR '.join(quote(t) for t in terms)})"
                )
                add_unique(exclude_terms, seen_exclude, group, terms[0])
                continue
            for t in terms:
                key = normalize_term(t)
                if key and key not in seen_roles:
                    seen_roles.add(key)
                    role_terms.append(t)

        # ── Skills: requirements, so they AND — but only the important few ──
        # Rank by the same rule the TalentSearch term selection uses: skills
        # named in the primary role first (the core competency), then those
        # carrying an explicit years/recency requirement, then wizard chip
        # order. Keep the top N as hard ANDs and demote the overflow into the
        # preferred OR group rather than dropping it — it still lifts ranking
        # without gating the search.
        # Industry chips get their own AND'ed cluster and skip the must-skill cap:
        # industry and capability are different axes. Folding a preferred industry
        # into the shared OR bucket would emit
        # `(Articulate OR Captivate OR Healthcare)`, i.e. a healthcare candidate
        # with no eLearning tool satisfies the eLearning requirement — the merge
        # weakens the query instead of sharpening it. Recruiters write them as
        # separate groups for the same reason.
        industry_groups: List[str] = []
        seen_industry = set()

        must_skill_items: List[Dict[str, Any]] = []
        for item in criteria.skill_criteria or []:
            mt = match_type_of(item)
            terms = variants_of(item)
            if not terms:
                continue
            group = (
                quote(terms[0]) if len(terms) == 1
                else f"({' OR '.join(quote(t) for t in terms)})"
            )
            if mt == "exclude":
                add_unique(exclude_terms, seen_exclude, group, terms[0])
            elif term_is_industry(terms[0]):
                add_unique(industry_groups, seen_industry, group, terms[0])
            elif mt == "can":
                add_unique(can_terms, seen_can, group, terms[0])
            else:
                try:
                    has_years = int(item.get("years") or 0) > 0
                except (TypeError, ValueError):
                    has_years = False
                must_skill_items.append({
                    "group": group,
                    "value": terms[0],
                    "in_role": term_appears_as_token(terms[0], primary_role),
                    "weighted": has_years or bool(item.get("recent")),
                })

        must_skill_items.sort(
            key=lambda s: (0 if s["in_role"] else 1, 0 if s["weighted"] else 1)
        )
        for idx, s in enumerate(must_skill_items):
            if idx < must_skill_cap:
                add_unique(must_groups, seen_must, s["group"], s["value"])
            else:
                add_unique(can_terms, seen_can, s["group"], s["value"])

        for keyword in criteria.keywords:
            if keyword and keyword.strip():
                add_unique(must_groups, seen_must, quote(keyword), keyword)

        # ── Companies: alternatives too ("worked at any of these") ────────
        # These were ANDed, which asked for someone employed by every listed
        # company simultaneously.
        company_terms: List[str] = []
        seen_companies = set()
        for company in criteria.companies:
            c = str(company or "").strip()
            key = normalize_term(c)
            if c and key and key not in seen_companies:
                seen_companies.add(key)
                source_keys.add(key)
                company_terms.append(c)

        parts: List[str] = []
        parts.extend(must_groups)
        # Own AND'ed clause(s), never merged into the preferred bucket.
        parts.extend(industry_groups)
        if company_terms:
            parts.append(
                quote(company_terms[0]) if len(company_terms) == 1
                else f"({' OR '.join(quote(c) for c in company_terms)})"
            )
        if can_terms:
            # Singletons are flattened — `(A)` and `A` are equivalent, and the
            # bare form reads better in the string the recruiter copies.
            parts.append(
                can_terms[0] if len(can_terms) == 1
                else f"({' OR '.join(can_terms)})"
            )

        is_jobdiva = str(dialect or "generic").strip().lower() == "jobdiva"

        # Roles. In JobDiva's dialect they ride in the dedicated TITLES= field,
        # which matches the candidate's job title rather than anywhere in the
        # résumé body — so a keyword-chain role group both over-matches (someone
        # who merely mentions the title) and competes with the skill ANDs. Every
        # other consumer (Unipile, Exa) only understands a plain expression, so
        # there the role group is AND'ed into the chain as before.
        titles_suffix = ""
        if role_terms:
            role_clause = " OR ".join(quote(t) for t in role_terms)
            if is_jobdiva:
                titles_suffix = f" , TITLES= ({role_clause})"
            else:
                parts.insert(
                    0,
                    quote(role_terms[0]) if len(role_terms) == 1 else f"({role_clause})",
                )

        # Geo. A remote role must NOT carry a city keyword: `AND "Dallas, TX"` is
        # a literal résumé-text match, so on a 100%-remote job it rejects every
        # candidate who doesn't happen to name that city — the opposite of the
        # intent. JobDiva has a structured country filter for this (`IN {US}`,
        # what recruiters hand-write); other dialects have no equivalent, so we
        # simply omit the constraint and let the caller's own location argument
        # and the client-side location scorer handle it.
        location_type = str(getattr(criteria, "location_type", "") or "").strip().lower()
        if location_type == "remote":
            if is_jobdiva:
                parts.append("IN {US}")
        elif criteria.location:
            # Multi-location: combine primary + additional locations as OR in
            # the boolean string. For JobDiva TalentSearch the per-location loop
            # in _search_jobdiva_talent_search already passes geo/zip params per
            # location, so the combined text clause is intentionally omitted in
            # those calls (alt_criteria has additional_locations=[]). This path
            # only fires for the fallback single-criteria call and non-JobDiva
            # dialects where a text OR is the only way to express multi-location.
            loc_parts = [criteria.location]
            for loc in criteria.additional_locations:
                v = str(loc.value or "").strip()
                if v and v not in loc_parts:
                    loc_parts.append(v)
            if len(loc_parts) == 1:
                add_unique(parts, seen_must, quote(criteria.location), criteria.location)
            else:
                loc_clause = "(" + " OR ".join(quote(lp) for lp in loc_parts) + ")"
                parts.append(loc_clause)

        boolean_string = " AND ".join(part for part in parts if part and part != "()") or "*"
        if exclude_terms:
            boolean_string = f"{boolean_string} NOT ({' OR '.join(exclude_terms)})"

        logger.info(f"Boolean string built from Page 5 sourcing filters only: {boolean_string[:150]}...")
        return boolean_string + titles_suffix

    def _filter_candidates(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
        source_type: str = "applicants",
    ) -> List[Dict[str, Any]]:
        dimensions = self._collect_sourcing_dimensions(criteria)  # Use sourcing dimensions for pre-screening
        enforce_location = self._should_enforce_location(criteria)
        filterable_dimensions = [
            dimension for dimension in dimensions
            if dimension["label"] in {"Titles", "Skills", "Location", "Company Experience", "Keywords"}
        ]
        if not any(
            dimension["required"] or dimension["preferred"] or dimension["excluded"]
            for dimension in filterable_dimensions
        ):
            return candidates

        title_groups: List[List[str]] = []
        skill_groups: List[List[str]] = []
        company_groups: List[List[str]] = []
        keyword_groups: List[List[str]] = []
        exclude_groups: List[List[str]] = []

        for dimension in filterable_dimensions:
            exclude_groups.extend(dimension.get("excluded_groups", []))
            if dimension["label"] == "Titles":
                title_groups.extend(dimension.get("required_groups", []))
            elif dimension["label"] == "Skills":
                skill_groups.extend(dimension.get("required_groups", []))
            elif dimension["label"] == "Company Experience":
                company_groups.extend(dimension.get("required_groups", []))
            elif dimension["label"] == "Keywords":
                keyword_groups.extend(dimension.get("required_groups", []))

        def group_matches(haystack: str, group: List[str]) -> bool:
            return any(term and term in haystack for term in self._dedupe_terms(group))

        filtered = []
        non_us_dropped = 0
        job_country = self._target_country(criteria)
        for candidate in candidates:
            haystack = self._candidate_summary_text(candidate)

            # Country scope: drop only when the candidate's country/location
            # text is positive evidence of a location outside the job's
            # country. Silent records are treated as in-country (kept).
            if self._is_likely_outside_country(candidate, job_country):
                non_us_dropped += 1
                continue

            if any(group_matches(haystack, group) for group in exclude_groups):
                continue

            # Location is no longer a hard pre-enrichment drop. Policy update
            # 2026-05-13: when in doubt, surface the candidate with a score
            # signal instead of silently rejecting. Recruiter's UI filter
            # handles final location selection. Distance / state-mismatch
            # still flow into screening_summary and the score dimension.

            # Titles are alternative labels for the role, so one matching title is
            # enough at the pre-enrichment stage.
            if title_groups and self._has_visible_field(candidate, ["title", "headline"]):
                if not any(group_matches(haystack, group) for group in title_groups):
                    continue

            # Skills can be sparse in applicant/TalentSearch summaries. If JobDiva
            # returned skill text, use it now; otherwise defer skill proof to the
            # LLM-enriched resume screen.
            if skill_groups and self._candidate_has_visible_skills(candidate):
                if not all(group_matches(haystack, group) for group in skill_groups):
                    continue

            if company_groups and self._has_visible_field(candidate, ["company", "employer", "current_company"]):
                if not all(group_matches(haystack, group) for group in company_groups):
                    continue

            if keyword_groups and source_type == "talent_search":
                if not all(group_matches(haystack, group) for group in keyword_groups):
                    continue

            filtered.append(candidate)

        self._log_stage(
            "SummaryScreen",
            f"{source_type}: kept {len(filtered)} of {len(candidates)} candidate(s)"
            f" (non_us_dropped={non_us_dropped})",
        )
        return filtered

    def _candidate_summary_text(self, candidate: Dict[str, Any]) -> str:
        skills = candidate.get("skills", []) or []
        skill_text = json.dumps(skills) if not isinstance(skills, str) else skills
        pieces = [
            candidate.get("name", ""),
            candidate.get("firstName", ""),
            candidate.get("lastName", ""),
            candidate.get("title", ""),
            candidate.get("headline", ""),
            candidate.get("city", ""),
            candidate.get("state", ""),
            candidate.get("location", ""),
            candidate.get("company", ""),
            candidate.get("employer", ""),
            candidate.get("current_company", ""),
            skill_text,
        ]
        return self._normalize_search_text(" ".join(str(piece) for piece in pieces if piece))

    def _normalize_search_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").lower()).strip()

    def _has_visible_field(self, candidate: Dict[str, Any], field_names: List[str]) -> bool:
        return any(str(candidate.get(field) or "").strip() for field in field_names)

    def _candidate_has_visible_skills(self, candidate: Dict[str, Any]) -> bool:
        skills = candidate.get("skills")
        if isinstance(skills, list) and len(skills) > 0:
            return True
        return bool(str(skills or "").strip())
