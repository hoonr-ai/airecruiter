"""Closures of UnifiedCandidateSearch._search_candidates_impl, moved out verbatim.

Each takes the search run's shared state (the names it used to close over)
as keyword arguments; the method keeps a thin wrapper that passes them.
"""
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from utils.email_utils import is_placeholder_email
from services.exa_service import _extract_city_from_highlights
from services.exa_errors import ExaOutOfCreditsError
from services.location import sanitize_candidate_location
from services.no_contact import apply_no_contact_flag
from services import contact_enrichment
from services.profile_resume import deep_search_profile, normalize_linkedin_profile
from .common import (
    _richer_profile_location,
    logger,
)




async def produce_external(name, search_method, *, SENTINEL, _emit_sample_best, criteria, emit_candidate, emit_source_status, enrich_shown_row, exa_pass_a_done, exa_pass_b_should_run, queue, sample_mode, sample_pool, self, summary):
    # Status label must match the source string stamped on rows, which
    # is only known after the search returns — fall back to the static
    # mapping when the search fails or yields nothing.
    _ext_label = {"LinkedIn": "LinkedIn-Unipile", "Exa": "LinkedIn-Exa"}.get(name, name)
    _ext_raw = 0
    _ext_emitted = 0
    _ext_error: Optional[BaseException] = None
    # Post-emit contact lookups; drained before this producer's SENTINEL
    # so their patches land inside the stream.
    ext_contact_tasks: List[asyncio.Task] = []
    try:
        await queue.put({"type": "stage", "data": f"Searching {name}..."})
        res = await search_method(criteria)
        if not res:
            return
        ext_candidates = res.get("candidates", [])
        source_type = res.get("source_type", name)
        _ext_label = source_type
        _ext_raw = len(ext_candidates)
        summary[f"{source_type.lower()}_count"] = len(ext_candidates)

        # HOTFIX: Hard cap at 100 — see Applicants stage above.
        # F5: pre-rank by cheap title/skill keyword match before
        # slicing. External sources (Exa/Dice/Unipile) ship enough
        # signal in `title` + highlight text to rank meaningfully,
        # and an unranked FIFO slice can discard the best matches
        # if Exa returns ordered by its own relevance and we ask
        # for 50 results but the cap fires elsewhere.
        _ext_keep_top = min(100, sample_pool) if sample_mode else 100
        if ext_candidates and len(ext_candidates) > _ext_keep_top:
            before_rank = len(ext_candidates)
            ext_candidates = self._rank_candidates_by_skill(
                ext_candidates, criteria, keep_top=_ext_keep_top
            )
            self._log_stage(
                source_type,
                f"Capping {before_rank} {source_type} profiles to "
                f"top-{len(ext_candidates)} by skill+title rank.",
            )

        self._log_stage(source_type, f"Found {len(ext_candidates)} profiles; starting streaming enrichment...")

        from core import sourcing_config as _sc_ext
        semaphore = asyncio.Semaphore(
            max(1, int(getattr(_sc_ext, "EXTERNAL_PROCESS_CONCURRENCY", 12) or 12))
        )

        async def _process_external_single(cand):
            async with semaphore:
                cand["source"] = source_type

                # F3: cheap role-anchor check on external candidates.
                # Exa has no real query filter — surfaced profiles can
                # be completely off-role (e.g. software engineers when
                # the job is "Program Manager"). Drop those before
                # they consume a 100-cap slot and a downstream LLM call.
                # Unipile is checked AFTER profile enrichment fills its
                # title field; Exa/Dice carry a title from the start.
                if source_type != "LinkedIn-Unipile" and not self._candidate_title_match(cand, criteria):
                    return {"status": "failed_title_match"}

                is_linkedin = source_type.startswith("LinkedIn")
                if source_type == "LinkedIn-Unipile":
                    provider_id = cand.get("provider_id")
                    if provider_id:
                        try:
                            full_profile = await self.unipile_service.get_candidate_profile(
                                provider_id, account_id=cand.get("unipile_account_id")
                            )
                            if full_profile:
                                # The search row's `title` is the
                                # LinkedIn headline; keep it reachable
                                # for the role-anchor check when the
                                # profile supplies a real job title.
                                _headline = str(cand.get("title") or "")
                                cand.update(self._extract_linkedin_profile_data(full_profile))
                                if _headline and not cand.get("headline"):
                                    cand["headline"] = _headline
                                # The WHOLE profile (role descriptions,
                                # summary, languages ...) rides to
                                # /candidates/save so Launch PAIR can give
                                # JobDiva a complete résumé; the fields
                                # above are only what scoring reads.
                                _linkedin_profile = normalize_linkedin_profile(full_profile)
                                if _linkedin_profile:
                                    cand["linkedin_profile"] = _linkedin_profile
                                    # Contact the member shares on LinkedIn
                                    # is free -- take it before any paid
                                    # ZoomInfo / Apollo / Exa lookup runs.
                                    _li_email, _li_phone = contact_enrichment.sanitize_agent_contact(
                                        (_linkedin_profile.get("emails") or [""])[0],
                                        (_linkedin_profile.get("phones") or [""])[0],
                                    )
                                    if _li_email and not is_placeholder_email(_li_email) and not str(cand.get("email") or "").strip():
                                        cand["email"] = _li_email
                                    if _li_phone and not str(cand.get("phone") or "").strip():
                                        cand["phone"] = _li_phone
                        except Exception as e:
                            logger.warning(f"Failed to fetch full profile for LinkedIn candidate {provider_id}: {e}")
                    # After enrichment, run the same role-anchor check.
                    if not self._candidate_title_match(cand, criteria):
                        return {"status": "failed_title_match"}

                # PR-B: cheap pre-LLM YOE gate for external sources too.
                # Drops candidates whose headline / abstract / resume
                # snippet shows fewer years than the configured floor.
                if self._candidate_outside_years_range_pre_llm(cand, criteria):
                    return {"status": "failed_filter"}
                assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                if not assessment["passes"]:
                    return {"status": "failed_filter"}

                # No-contact company (checked after the Unipile profile
                # fetch fills company_experience): emit the row so Step 5
                # renders it greyed out, but spend nothing more on it —
                # no LLM extraction (which is also the enhanced-info
                # persistence path), no deep analysis, no paid contact
                # enrichment.
                if apply_no_contact_flag(cand):
                    cand["enhanced_info"] = cand.get("enhanced_info") or {}
                    cand["enhanced_info_status"] = "no_contact"
                    return {"status": "no_contact_shown", "candidate": cand}

                from services.sourced_candidates_storage import process_linkedin_candidate, process_dice_candidate
                if is_linkedin:
                    enhanced = await process_linkedin_candidate(cand)
                elif source_type == "Dice":
                    enhanced = await process_dice_candidate(cand)
                else:
                    enhanced = cand

                if isinstance(enhanced, dict) and enhanced is not cand:
                    cand["enhanced_info"] = enhanced.get("raw", enhanced)
                else:
                    cand["enhanced_info"] = cand.get("enhanced_info") or {}

                cand["enhanced_info_status"] = "completed"
                cand["name"] = cand["enhanced_info"].get("candidate_name") or cand.get("name")
                # For LinkedIn-Exa, ZoomInfo→Apollo is the sole source of
                # truth for email/phone — enhanced_info values here are
                # LLM-extracted from profile text and frequently empty or
                # hallucinated. Skipping the assignment lets the inline
                # enrichment block below populate contact info cleanly.
                if source_type != "LinkedIn-Exa":
                    cand["email"] = cand["enhanced_info"].get("email") or cand.get("email")
                    cand["phone"] = cand["enhanced_info"].get("phone") or cand.get("phone")
                cand["title"] = cand["enhanced_info"].get("job_title") or cand.get("title")
                cand["experience_years"] = (
                    cand["enhanced_info"].get("years_of_experience")
                    or cand.get("experience_years")
                )
                # Résumé is final for residence (see
                # _apply_resume_location): an explicitly stated
                # résumé location replaces the profile's; the profile
                # value only stands when the résumé is silent.
                self._apply_resume_location(cand)
                # The heuristic gate above is only an early cost saver.
                # Once enrichment has extracted total YOE, enforce the
                # requested range against that authoritative value.
                range_assessment = self._filter_assessment(
                    cand, criteria, enforce_years=True
                )
                if (
                    range_assessment.get("min_years_failure")
                    or range_assessment.get("max_years_failure")
                ):
                    self._log_stage(
                        "ExperienceGate",
                        "dropping candidate_id=%s outside configured experience range" % (
                            cand.get("candidate_id") or cand.get("id"),
                        ),
                    )
                    return {"status": "failed_experience_range"}
                if cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills"):
                    cand["skills"] = cand["enhanced_info"].get("structured_skills") or cand["enhanced_info"].get("skills")

                # Exa deep analysis on filter survivors only. Replaces
                # the 4000-char highlights with the full profile text
                # plus a per-candidate match summary; preserves the
                # original highlights in resume_text since downstream
                # location extractors are tuned to that shape.
                # When Pass B (the Exa Agent run) is on it supersedes
                # this per-URL get_contents() summary. Both are paid
                # and both default off (sourcing_config): turning Pass B
                # off must not silently turn this on.
                from core import sourcing_config as _sc_deep
                if (
                    source_type == "LinkedIn-Exa"
                    and not sample_mode
                    and not exa_pass_b_should_run
                    and _sc_deep.exa_deep_analysis_enabled()
                ):
                    try:
                        deep = await self.exa_service.deep_analyze_candidate(
                            str(cand.get("profile_url") or ""),
                            criteria.sourcing_skill_values(),
                            criteria.location or "",
                        )
                    except Exception as e:
                        logger.warning("Exa deep_analyze raised for %s: %s", cand.get("id"), e)
                        deep = {}
                    if deep:
                        cand["deep_text"] = deep.get("text", "")
                        cand["exa_deep_summary"] = deep.get("summary", "")
                        if (not cand.get("city") or not cand.get("state")) and deep.get("text"):
                            head = deep["text"][:600]
                            c2, s2 = _extract_city_from_highlights(head)
                            if c2 and not cand.get("city"):
                                cand["city"] = c2
                            if s2 and not cand.get("state"):
                                cand["state"] = s2

                # In-line ZoomInfo → Apollo (→ Exa Agent for LinkedIn
                # sources) enrichment.
                #   - LinkedIn-Exa: ALWAYS run; the result overwrites any
                #     pre-existing email/phone. The enrichment chain is
                #     the source of truth for Exa-sourced contact info.
                #   - Other sources (Dice/Unipile): run as a backfill
                #     whenever email OR phone is missing — Unipile never
                #     supplies contact itself, so ZoomInfo/Apollo (and
                #     the Exa fallback for LinkedIn-*) fill the gaps;
                #     only empty fields are written (overwrite=False).
                # Gated by CONTACT_ENRICHMENT_INLINE_ENABLED inside the
                # helper; capped per-job at contact_enrichment.PER_JOB_CAP
                # (+ EXA_SOURCING_CONTACT_CAP for the Exa fallback).
                #
                # `full_name` is required for the ZoomInfo path — the
                # new Data API doesn't accept linkedinUrl as a match
                # input, so we need firstName + lastName for
                # ContactSearch. Without a name we skip ZoomInfo and
                # go straight to Apollo (which does accept a URL).
                is_exa_source = source_type == "LinkedIn-Exa"
                # EMAIL is the only field sourcing spends on. 436 widened
                # this to "either field missing", which pulled every
                # phone-less candidate into the provider chain — and since
                # phones are the costly half (Apollo per-record reveal, Exa
                # per run), that was the bulk of sourcing enrichment spend,
                # incurred for candidates the recruiter had not shortlisted
                # yet. Phone acquisition now happens at Launch PAIR or on
                # the Step 5 phone button. The helper is also told
                # want_phone=False, so this is belt-and-braces.
                has_email = bool(str(cand.get("email") or "").strip())
                # Sample mode: never spend paid enrichment credits on a
                # preview row — contact info is acquired in the full
                # run / at Launch PAIR for the rows that matter.
                # The lookup itself runs AFTER the row is emitted (see
                # enrich_shown_row below): inline, it held every Exa row
                # off screen for the whole ZoomInfo → Apollo → Exa chain
                # and bought contacts for rows the score gate then dropped.
                needs_contact = (is_exa_source or not has_email) and not sample_mode
                return {"status": "success", "candidate": cand, "needs_contact": needs_contact}

        process_tasks = [asyncio.create_task(_process_external_single(c)) for c in ext_candidates]
        # Per-status drop accounting: these gates used to discard rows
        # with no counter or log line, so "Exa found 50, UI shows 1"
        # was undiagnosable from the stream log.
        _status_counts: Dict[str, int] = {}
        # Sample mode: hold rows until the whole probe pool is
        # scored, then emit the best ones (_emit_sample_best).
        _ext_sample_rows: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        try:
            for task in asyncio.as_completed(process_tasks):
                result = await task
                _status_counts[result["status"]] = _status_counts.get(result["status"], 0) + 1
                if result["status"] in ("success", "no_contact_shown"):
                    cand = result["candidate"]
                    assessment = self._filter_assessment(cand, criteria, enforce_years=False)
                    if sample_mode:
                        _ext_sample_rows.append((cand, assessment))
                        continue
                    if await emit_candidate(cand, assessment):
                        _ext_emitted += 1
                        if result.get("needs_contact"):
                            ext_contact_tasks.append(asyncio.create_task(
                                enrich_shown_row(cand, overwrite=source_type == "LinkedIn-Exa")
                            ))
        finally:
            # An exception mid-loop can leave pool tasks in flight —
            # cancel and drain so no work leaks past the producer.
            # No-op when the loop ran to completion.
            for _t in process_tasks:
                if not _t.done():
                    _t.cancel()
            await asyncio.gather(*process_tasks, return_exceptions=True)
        if sample_mode:
            _ext_emitted = await _emit_sample_best(
                _ext_sample_rows, emit_candidate, source_type
            )
        if _status_counts:
            self._log_stage(
                name,
                f"processed {len(ext_candidates)} {source_type} candidates: "
                + ", ".join(f"{k}={v}" for k, v in sorted(_status_counts.items())),
            )
    except ExaOutOfCreditsError as e:
        # Already logged once at ERROR by the breaker; no traceback.
        _ext_error = e
        logger.warning(f"{name} search skipped: {e}")
    except Exception as e:
        _ext_error = e
        logger.error(f"{name} search stage failed: {e}", exc_info=True)
    finally:
        await emit_source_status(
            _ext_label,
            count=_ext_emitted,
            error=_ext_error,
            empty_reason=(
                f"{_ext_label} returned {_ext_raw} profiles, but none cleared "
                "the relevance/score gates or all were already listed."
                if _ext_raw > 0
                else f"{_ext_label} returned no matching profiles — the source "
                "may have no matches, be disabled, or be out of quota."
            ),
        )
        # Signal Pass A completion so the Exa Research producer can
        # seed its run with the URLs we just yielded — before waiting
        # on contact lookups, which Pass B does not need.
        if name == "Exa" and exa_pass_b_should_run:
            exa_pass_a_done.set()
        if ext_contact_tasks:
            try:
                await asyncio.gather(*ext_contact_tasks, return_exceptions=True)
            finally:
                for _t in ext_contact_tasks:
                    if not _t.done():
                        _t.cancel()
        await queue.put(SENTINEL)


async def produce_exa_agent(*, SENTINEL, criteria, dedup_owner, emit_candidate, enrich_shown_row, exa_pass_a_done, exa_yielded_candidates, queue, self):
    """Pass B: Exa Agent API (Websets 2.0) deep-search.

            Waits for Pass A (LinkedIn-Exa producer) to finish, snapshots the
            URLs it yielded, runs one Agent call that BOTH enriches those
            URLs with structured fields (last_activity, follower_count,
            recent_companies, fit_rationale) AND discovers additional
            candidates. URL-overlapping results become `candidate_detail`
            patches with stage `exa_deep_search`; new ones become fresh
            `candidate` events tagged `LinkedIn-DeepSearch`.

            Emits stage events at start / completion / error so the Step-5 UI
            can show "Exa deep-search ran (N enriched, M new)" or "Exa
            deep-search failed — falling back to keyword results" in the
            status bar without the recruiter having to inspect API logs.

            Contact info is NOT requested from the agent (see
            EXA_AGENT_CONTACT_FIELDS): its contact tool billed a lookup for
            every returned profile before Apollo was ever tried. New rows get
            the regular Apollo-first chain instead, after they are shown.
            """
    enriched = 0
    new_found = 0
    failure_reason = ""
    contact_tasks: List[asyncio.Task] = []

    try:
        await queue.put({"type": "stage", "data": "Exa deep-search warming up..."})
        # Bounded wait so a hung Pass A can't stall Pass B forever.
        try:
            await asyncio.wait_for(exa_pass_a_done.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            logger.warning("Exa Pass A did not signal within 120s — running deep-research with no seeds")

        seed_urls: List[str] = []
        for c in exa_yielded_candidates:
            u = str(c.get("profile_url") or "").strip()
            if u:
                seed_urls.append(u)

        await queue.put({
            "type": "stage",
            "data": f"Exa deep-search running ({len(seed_urls)} seeds)...",
        })

        async with self._exa_agent_semaphore:
            try:
                # Plain title text — the agent query is natural
                # language, so no quoted-boolean role hints here.
                # jd_role joins the top titles with " or " and is the
                # string the agent prompt prefers. (criteria has no
                # `role_hint` field; the old `criteria.role_hint`
                # fallback raised AttributeError whenever
                # title_criteria was empty and silently killed Pass B.)
                agent_titles = criteria.sourcing_titles()
                results = await self.exa_service.deep_research_candidates(
                    jd_title=agent_titles[0] if agent_titles else "",
                    jd_role=" or ".join(agent_titles[:2]),
                    skills=criteria.skill_only_values(),
                    # Remote-aware: US-wide for remote jobs; else all
                    # configured locations "or"-joined (Pass B is one
                    # call per request, so additional locations ride
                    # in the same prompt rather than a fan-out).
                    location=self._search_location_for_deep_research(criteria),
                    seed_urls=seed_urls,
                    # Exa accepts one radius for this OR query, so
                    # request the broadest configured search envelope.
                    # emit_candidate enforces each chip's exact radius
                    # before a candidate reaches Step 5.
                    within_miles=max(
                        [getattr(criteria, "within_miles", 25) or 25]
                        + [entry.within_miles for entry in (criteria.additional_locations or [])]
                    ),
                    exclude_company=str(getattr(criteria, "client_name", "") or ""),
                )
            except Exception as e:
                logger.warning("deep_research_candidates raised: %s", e)
                failure_reason = f"agent call raised: {type(e).__name__}"
                results = []

        if not results:
            if not failure_reason:
                failure_reason = "agent returned no candidates"
            return

        def _norm_url(u: Any) -> str:
            s = str(u or "").strip().lower()
            if not s:
                return ""
            return s.split("?", 1)[0].rstrip("/")

        pass_a_by_url: Dict[str, Dict[str, Any]] = {
            _norm_url(c.get("profile_url")): c
            for c in exa_yielded_candidates
            if c.get("profile_url")
        }

        for idx, entry in enumerate(results):
            if not isinstance(entry, dict):
                continue
            url = entry.get("linkedin_url") or ""
            nurl = _norm_url(url)
            patch_fields: Dict[str, Any] = {
                "exa_last_activity": entry.get("last_activity"),
                "exa_follower_count": entry.get("follower_count"),
                "exa_recent_companies": entry.get("recent_companies") or [],
                "exa_fit_rationale": entry.get("fit_rationale") or "",
            }
            # Contact fields from the agent's enrichment tool — only
            # present when EXA_AGENT_CONTACT_FIELDS re-enables them
            # (off by default: Apollo gets first refusal). Sanity-gate
            # before use; only backfill — never overwrite ZoomInfo/Apollo
            # data already on the row.
            agent_email, agent_phone = contact_enrichment.sanitize_agent_contact(
                entry.get("email"), entry.get("phone")
            )

            if nurl and nurl in pass_a_by_url:
                existing = pass_a_by_url[nurl]
                cid = str(existing.get("candidate_id") or existing.get("id") or "")
                if not cid:
                    continue
                # Pass A can store a blank line or the wrong city.
                # The agent reads the profile line. Write that place
                # onto the row. If it is outside every circle or
                # outside the country, remove the card: Step 5 already
                # drops a row when this event arrives with
                # stage "dropped". A place inside a circle, or a vague
                # label, stays and the badge updates.
                recovered_location = sanitize_candidate_location(entry.get("location"))
                current_location = sanitize_candidate_location(existing.get("location"))
                # A later read of the profile can replace a bare city
                # ("Edison") with the line LinkedIn actually shows
                # ("Edison, New Jersey, United States").
                upgraded = _richer_profile_location(current_location, recovered_location)
                if upgraded:
                    existing["location"] = upgraded
                    recovered_location = upgraded
                    outside_country = self._is_likely_outside_country(
                        existing, self._target_country(criteria)
                    )
                    recovered_veto = None if outside_country else self._location_hard_gate(
                        existing, criteria
                    )
                    if outside_country or recovered_veto:
                        self._log_stage(
                            "LocationGate",
                            f"dropping candidate_id={cid} after Exa location recovery "
                            f"location={recovered_location!r} "
                            f"reason={existing.get('location_veto_reason') or 'non_us_candidate'}",
                        )
                        await queue.put({
                            "type": "candidate_detail",
                            "candidate_id": cid,
                            "stage": "dropped",
                            "patch": {
                                "_stage": "dropped",
                                "_drop_reason": (
                                    "non_us_candidate" if outside_country else "location_mismatch"
                                ),
                            },
                        })
                        continue
                    patch_fields["location"] = recovered_location
                    # A confirmed in-radius place clears the
                    # unverified badge the blank row was showing.
                    patch_fields["location_match_reason"] = (
                        existing.get("location_match_reason") or ""
                    )
                    if existing.get("distance_miles") is not None:
                        patch_fields["distance_miles"] = existing["distance_miles"]
                prior_sources = existing.get("sources") or [existing.get("source") or "LinkedIn-Exa"]
                if not isinstance(prior_sources, list):
                    prior_sources = [str(prior_sources)]
                merged = list(dict.fromkeys([*prior_sources, "LinkedIn-DeepSearch"]))
                patch_fields["sources"] = merged
                patch_fields["_stage"] = "exa_deep_search"
                if agent_email and not str(existing.get("email") or "").strip():
                    patch_fields["email"] = agent_email
                if agent_phone and not str(existing.get("phone") or "").strip():
                    patch_fields["phone"] = agent_phone
                existing["sources"] = merged
                existing.update({k: v for k, v in patch_fields.items() if v is not None})
                # Deep-search may reveal the employer (recent_companies)
                # only now, after the row was emitted unflagged —
                # re-check and ride the flag on this same patch.
                if apply_no_contact_flag(existing):
                    for _nc_key in ("no_contact", "no_contact_reason", "no_contact_company"):
                        if existing.get(_nc_key) is not None:
                            patch_fields[_nc_key] = existing[_nc_key]
                await queue.put({
                    "type": "candidate_detail",
                    "candidate_id": cid,
                    "stage": "exa_deep_search",
                    "patch": patch_fields,
                })
                enriched += 1
            else:
                new_id = f"exadeep_{idx}_{nurl[-32:] or 'unknown'}"
                # Pull skills out of recent_companies / fit_rationale
                # so the scorer has something to compare against the
                # JD's required skills. Without this, every deep-only
                # candidate scored 0 and sank to the bottom of the
                # sorted list — invisible to the recruiter.
                rationale_blob = " ".join([
                    str(entry.get("fit_rationale") or ""),
                    *[
                        f"{rc.get('title', '')} {rc.get('company', '')}"
                        for rc in (entry.get("recent_companies") or [])
                        if isinstance(rc, dict)
                    ],
                ])
                new_cand: Dict[str, Any] = {
                    "id": new_id,
                    "candidate_id": new_id,
                    "provider_id": new_id,
                    "name": entry.get("name") or "",
                    "firstName": (str(entry.get("name") or "").split(" ", 1) + [""])[0],
                    "lastName": (str(entry.get("name") or "").split(" ", 1) + [""])[1],
                    "title": entry.get("current_title") or "",
                    "headline": entry.get("current_title") or "",
                    "location": entry.get("location") or "",
                    "email": agent_email,
                    "phone": agent_phone,
                    "profile_url": url,
                    "source": "LinkedIn-DeepSearch",
                    "sources": ["LinkedIn-DeepSearch"],
                    "skills": [],
                    "resume_text": rationale_blob,  # feeds the scorer
                    # The person's own profile facts (title, location,
                    # recent roles) for the JobDiva résumé -- the
                    # rationale above is PAIR's words and never goes there.
                    "linkedin_profile": deep_search_profile(entry),
                    "_stage": "exa_deep_search",
                    **{k: v for k, v in patch_fields.items() if v is not None},
                }
                # Hard filter: never surface someone currently employed
                # by the hiring client. The agent prompt already asks
                # for the exclusion, but its recent_companies output is
                # the ground truth — belt and suspenders.
                try:
                    from services.company_match import currently_employed_by_client
                    _client_match = currently_employed_by_client(
                        new_cand, str(getattr(criteria, "client_name", "") or "")
                    )
                    if _client_match:
                        logger.info(
                            "Exa deep-search dropping %s — currently at hiring client (%s)",
                            new_cand.get("name"), _client_match,
                        )
                        continue
                except Exception:
                    pass

                # Cross-source dedup so a deep-search hit doesn't
                # duplicate a row that some other producer (Unipile,
                # JobDiva) already emitted under a different id. On a
                # collision, fold this hit's best-of into the owner
                # rather than dropping it on the floor.
                keys = self._dedup_keys(new_cand)
                _deep_owner = next((dedup_owner[k] for k in keys if k in dedup_owner), None)
                if _deep_owner is not None:
                    self._merge_candidate_best_of(_deep_owner, new_cand)
                    continue
                # Route through emit_candidate so the deep-only
                # candidate gets the same scoring + finalize_candidate
                # treatment as Pass A — without this its match_score
                # stayed at 0 and it sorted to the bottom of the list.
                # The closure already handles seen_ids / dedup_owner
                # bookkeeping internally; we don't pre-add here.
                try:
                    assessment = self._filter_assessment(
                        new_cand, criteria, enforce_years=False
                    )
                except Exception as e:
                    logger.warning("filter_assessment failed for deep candidate %s: %s", new_id, e)
                    assessment = {
                        "passes": True, "matched": [], "missing": [],
                        "excluded": [], "score": 0,
                    }
                # Deep-only candidates never pass through the Pass A
                # enrichment block, so they get the same ZoomInfo →
                # Apollo → (Exa on a miss) chain here — but only once
                # emit_candidate has shown them. Rows the score/location
                # gates drop, and no-contact-company rows, cost nothing.
                if not await emit_candidate(new_cand, assessment):
                    continue
                new_found += 1
                if not new_cand.get("no_contact") and not (
                    str(new_cand.get("email") or "").strip()
                    and str(new_cand.get("phone") or "").strip()
                ):
                    contact_tasks.append(
                        asyncio.create_task(enrich_shown_row(new_cand, overwrite=True))
                    )
        # Patches must land before this producer's SENTINEL closes
        # its share of the stream.
        if contact_tasks:
            await asyncio.gather(*contact_tasks, return_exceptions=True)
    except Exception as e:
        logger.error(f"Exa Agent producer failed: {e}", exc_info=True)
        failure_reason = failure_reason or f"producer crashed: {type(e).__name__}"
    finally:
        for task in contact_tasks:
            if not task.done():
                task.cancel()
        # Final status emit so the UI status bar reflects what
        # happened, even when no candidate_detail patches landed.
        if failure_reason and (enriched + new_found) == 0:
            await queue.put({
                "type": "stage",
                "data": f"Exa deep-search skipped: {failure_reason}",
            })
        else:
            await queue.put({
                "type": "stage",
                "data": f"Exa deep-search done — {enriched} enriched, {new_found} new",
            })
        self._log_stage(
            "ExaAgent",
            f"pass_b finished enriched={enriched} new_found={new_found} "
            f"failure_reason={failure_reason or '(none)'}",
        )
        await queue.put(SENTINEL)
