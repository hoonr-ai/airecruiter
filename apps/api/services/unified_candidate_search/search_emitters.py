"""Closures of UnifiedCandidateSearch._search_candidates_impl, moved out verbatim.

Each takes the search run's shared state (the names it used to close over)
as keyword arguments; the method keeps a thin wrapper that passes them.
"""
from typing import List, Dict, Any, Optional, Tuple
from core import pipeline_trace as _pipeline_trace
from services.exa_errors import ExaOutOfCreditsError
from core.config import embedding_skill_match_for_family
from services import skill_embeddings
from services import contact_enrichment
from .common import (
    _paid_contact_deferred,
    logger,
)




async def emit_candidate(cand, assessment, qualified_counter_key=None, *, build_screening, criteria, dedup_owner, exa_pass_b_should_run, exa_yielded_candidates, finalize_candidate, queue, seen_ids, self, summary):
    # Keep the early source assessment permissive so candidates can
    # finish enrichment. The final location gate below enforces every
    # configured radius consistently before a row reaches Step 5.
    location_reason = assessment.get("location_failure_reason") if isinstance(assessment, dict) else None
    if not assessment.get("passes") and location_reason in {
        "non_us_candidate",
    }:
        distance = cand.get("distance_miles")
        self._log_stage(
            "LocationGate",
            f"dropping candidate_id={cand.get('candidate_id') or cand.get('id')} "
            f"reason={location_reason} distance={distance}",
        )
        return False

    cand["screening_summary"] = build_screening(assessment)

    # Warm candidate-side skill embeddings before scoring so the
    # sync `_fuzzy_term_score` path can read them from the
    # in-process cache. Honors both the global flag and the
    # per-family override (non-IT families default-on).
    if embedding_skill_match_for_family(self._current_family):
        try:
            cand_terms = self._candidate_skill_terms(cand)
            if cand_terms:
                await skill_embeddings.warm_terms(cand_terms)
        except Exception as exc:
            logger.warning(
                f"candidate-skill embedding warm failed: {exc}"
            )

    cand = finalize_candidate(cand)
    cid = str(cand.get("candidate_id") or cand.get("id"))

    # Step 5 has a strict radius contract across every source. Check
    # again after finalization because résumé extraction may replace
    # a stale profile location or recover a previously blank one.
    self._hydrate_candidate_location_from_zip(cand)
    final_location_veto = self._location_hard_gate(cand, criteria)
    if final_location_veto:
        summary["location_mismatch_dropped"] = summary.get("location_mismatch_dropped", 0) + 1
        self._log_stage(
            "LocationGate",
            f"dropping candidate_id={cid} source={cand.get('source')} "
            f"reason={cand.get('location_veto_reason') or final_location_veto} "
            f"distance={cand.get('distance_miles')}",
        )
        return False

    # Relevance gate for machine-queried sources (Exa / Unipile /
    # DeepSearch / Dice / Vetted). JobDiva-JobAgent (recruiter-authored
    # criteria) and JobDiva-Applicants (real applicants) are exempt.
    # Mirrors the TalentSearch ScoreGate: rows the recruiter must not
    # launch (confirmed wrong location, or below the score floor) are
    # never emitted. Unscoreable rows (match_score None) stay visible.
    from core import sourcing_config as _sc_gate
    _src = str(cand.get("source") or "")
    _exempt = set(getattr(_sc_gate, "EXTERNAL_MIN_SCORE_EXEMPT_SOURCES",
                          ("JobDiva-JobAgent", "JobDiva-Applicants")))
    if _src not in _exempt:
        _min_score = getattr(_sc_gate, "EXTERNAL_SOURCE_MIN_SCORE", None)
        if (
            _min_score is not None
            and cand.get("match_score") is not None
            and int(cand.get("match_score") or 0) < int(_min_score)
        ):
            summary["below_min_score_dropped"] = summary.get("below_min_score_dropped", 0) + 1
            self._log_stage(
                "ScoreGate",
                f"dropping candidate_id={cid} source={_src} "
                f"match_score={cand.get('match_score')} < min_score={_min_score}",
            )
            return False

    if cid and cid in seen_ids:
        return False
    cross_keys = self._dedup_keys(cand)
    owner = next((dedup_owner[k] for k in cross_keys if k in dedup_owner), None)
    if owner is not None:
        # Same person already shown (from this or another source).
        # Merge best-of into the surviving row instead of showing a
        # duplicate — and never emit a second row. If the survivor is
        # a JobDiva row it absorbs this source's contact info.
        changed = self._merge_candidate_best_of(owner, cand)
        owner_id = str(owner.get("candidate_id") or owner.get("id") or "")
        self._log_stage(
            "Dedup",
            f"merge-into-owner kept={owner_id}({owner.get('source')}) "
            f"dropped={cid}({cand.get('source')}) keys={cross_keys}",
        )
        if changed and owner_id:
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": owner_id,
                "patch": dict(changed),
            })
        return False
    if cid:
        seen_ids.add(cid)
    for k in cross_keys:
        dedup_owner[k] = cand
    if qualified_counter_key and assessment["passes"]:
        summary[qualified_counter_key] += 1
    summary["total_candidates"] += 1
    # Track Pass A's LinkedIn-Exa candidates so Pass B (Exa Research
    # API) can seed its instructions with their URLs and merge
    # enrichment patches back onto these exact dicts on overlap.
    if exa_pass_b_should_run and str(cand.get("source") or "") == "LinkedIn-Exa":
        exa_yielded_candidates.append(cand)
    await queue.put({"type": "candidate", "data": cand})
    return True


async def enrich_shown_row(cand: Dict[str, Any], *, overwrite: bool, criteria, dedup_owner, queue, self, summary):
    """Contact lookup (Apollo → paid Exa on a miss, emails only; see
            enrich_contact_for_sourcing) for a row that is ALREADY on screen;
            whatever it finds streams as a `contact_enrichment` patch.

            Running after emit means the score/location/dedup gates decide who
            gets a paid lookup, and the row paints without waiting on a lookup
            that can take most of a minute (Exa phone runs). On a search about
            to be launched the paid Exa step is left to Launch PAIR
            (``paid_deferred`` on the span, ``exa_contact_deferred`` in the
            summary)."""
    # Spend policy: LinkedIn sources at/above the score floor only.
    # emit_candidate's gate lets UNSCORED rows through for display, and
    # Unipile/Dice rows reach here too — neither may buy a lookup.
    blocked = contact_enrichment.contact_lookup_block_reason(
        cand.get("source"), cand.get("match_score")
    )
    if blocked:
        summary["contact_lookup_skipped"] = summary.get("contact_lookup_skipped", 0) + 1
        logger.debug("contact lookup skipped for %s: %s", cand.get("candidate_id") or cand.get("id"), blocked)
        return
    summary["contact_lookup_attempted"] = summary.get("contact_lookup_attempted", 0) + 1
    deferred = _paid_contact_deferred(cand, criteria)
    if deferred:
        summary["exa_contact_deferred"] = summary.get("exa_contact_deferred", 0) + 1
    before = {k: str(cand.get(k) or "").strip() for k in ("email", "phone")}
    async with _pipeline_trace.span(
        "contact_enrichment", source=str(cand.get("source") or ""), paid_deferred=deferred,
    ):
        await self._apply_contact_enrichment(cand, criteria, overwrite=overwrite)
    patch = {
        k: cand[k] for k in ("email", "phone")
        if str(cand.get(k) or "").strip() and str(cand.get(k)).strip() != before[k]
    }
    if not patch:
        return
    # The row was keyed for cross-source dedup before its contact
    # existed; later rows for the same person must still merge into it.
    for key in self._dedup_keys(cand):
        dedup_owner.setdefault(key, cand)
    await queue.put({
        "type": "candidate_detail",
        "candidate_id": str(cand.get("candidate_id") or cand.get("id") or ""),
        "stage": "contact_enrichment",
        "patch": patch,
    })


async def emit_jobdiva_agent_result(cand, source_label, *, criteria, queue, seen_ids, self, summary):
    """Stage 1 of progressive JobDiva flow: emit a minimal row from the
            agent search result so the UI can render an api_rank-ordered shimmer
            row before resume fetch / LLM extraction / scoring complete.

            Applies in-source candidate_id dedup only — cross-source dedup
            (email / linkedin / name+location) requires fields we don't have
            yet, so it runs at the ``scored`` stage instead. Returns True if
            the agent_result was emitted (caller should enrich); False if
            the candidate is a same-source duplicate.
            """
    cid = str(cand.get("candidate_id") or cand.get("id") or "")
    if cid and cid in seen_ids:
        return False

    self._hydrate_candidate_location_from_zip(cand)
    final_location_veto = self._location_hard_gate(cand, criteria)
    if final_location_veto:
        self._log_stage(
            "LocationGate",
            f"deferring JobDiva candidate_id={cid} source={source_label} "
            f"reason={cand.get('location_veto_reason') or final_location_veto} "
            f"distance={cand.get('distance_miles')}",
        )
        # The CRM location may be absent or stale; résumé enrichment
        # can supply the authoritative current residence. Keep it out
        # of Step 5 until the post-enrichment gate has a final verdict.
        cand["_defer_initial_emit"] = True

    if cid:
        seen_ids.add(cid)

    agent_payload: Dict[str, Any] = {
        "candidate_id": cid or cand.get("id"),
        "id": cand.get("id") or cid,
        "name": cand.get("name") or "",
        "source": cand.get("source") or source_label,
        "_stage": "agent_result",
    }
    # Pass through fields that the JobDiva agent typically populates
    # before enrichment so the row isn't fully empty on first paint.
    for key in (
        "api_rank", "location", "title", "headline", "email", "phone",
        "resume_id", "resume_text", "experience_years", "skills",
        "education", "certifications", "enhanced_info",
        "enhanced_info_status", "received", "city", "state",
        "linkedin_url", "profile_url", "image_url", "data",
        "zipcode", "distance_miles", "location_out_of_radius",
        "location_match_reason",
        "qualifications", "employee_status", "available",
        "availability_status", "current_company", "scoring_mode",
        "no_contact", "no_contact_reason", "no_contact_company",
        # Where the row's email came from when a merge gave it
        # another row's (_merge_candidate_best_of): Launch PAIR's
        # getCandidateSeedEmail reads it.
        "email_source",
    ):
        v = cand.get(key)
        if v not in (None, "", [], {}):
            agent_payload[key] = v

    if not cand.get("_defer_initial_emit", False):
        summary["total_candidates"] += 1
        await queue.put({"type": "candidate", "data": agent_payload})
    return True


async def emit_source_status(source: str, *, count: int, error: Optional[BaseException]=None, empty_reason: str='', skip_empty: bool=False, queue, **extra: Any):
    """Report how a source pool ended. Every pool fails open to zero
            rows with only a log line, so without this event the browser
            cannot tell a dead pool from a legitimately empty one. The
            exception is sanitized to its class name — raw error text can
            carry internal URLs/hosts and this reason renders verbatim in
            the recruiter-facing banner."""
    if isinstance(error, ExaOutOfCreditsError):
        # Not "no matches": the source could not be searched at all.
        status = "failed"
        reason = (
            f"Exa is out of credits — {source} results are missing "
            "until it is topped up."
        )
    elif error is not None:
        status = "failed"
        if count > 0:
            reason = (
                f"{source} search failed ({type(error).__name__}) after "
                f"{count} result(s) arrived — this list may be incomplete."
            )
        else:
            reason = (
                f"{source} search failed ({type(error).__name__}) — "
                "check the API logs."
            )
    elif count > 0:
        status, reason = "ok", ""
    elif skip_empty:
        # An exhausted follow-up tranche ("Search more") is not a source
        # problem — stay quiet rather than contradict rows already shown.
        return
    else:
        status = "empty"
        reason = empty_reason or f"{source} returned no results for this job."
    await queue.put({
        "type": "source_status",
        "data": {
            "source": source,
            "status": status,
            "count": count,
            "reason": reason,
            **extra,
        },
    })


async def _emit_sample_best(rows, emit_fn, stage_name: str, *, finalize_candidate, sample_cap, self):
    """Sample mode: the whole probe pool is scored first, then the
            best rows are emitted — always the top SAMPLE_MIN_ROWS_PER_SOURCE
            (a weak source still shows what it has), beyond that only rows
            at/above SAMPLE_QUALITY_FLOOR, never more than `sample_cap`.
            Hard-filter fails / no-contact / client-conflict rows never take
            a slot (the full run still shows them at 0%). `emit_fn` is the
            source's normal emitter (dedup, min-score gate, queue) and may
            decline a row, in which case the next-best one is tried.

            `rows` are (candidate, assessment) pairs collected from the
            source's enrichment stream instead of being emitted on arrival.
            """
    from core import sourcing_config as _sc_smp
    min_rows = max(0, int(getattr(_sc_smp, "SAMPLE_MIN_ROWS_PER_SOURCE", 2) or 0))
    floor = getattr(_sc_smp, "SAMPLE_QUALITY_FLOOR", None)
    scored: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for cand, assessment in rows:
        try:
            finalize_candidate(cand)
        except Exception as exc:  # scoring must never kill the stream
            logger.warning(
                f"sample pre-score failed for candidate_id={cand.get('candidate_id')}: {exc}"
            )
        scored.append((cand, assessment))

    def _sort_key(item):
        s = item[0].get("match_score")
        return -1.0 if s is None else float(s)

    scored.sort(key=_sort_key, reverse=True)
    emitted = 0
    skipped_hard = 0
    for cand, assessment in scored:
        if emitted >= sample_cap:
            break
        if self._sample_row_hard_failed(cand):
            skipped_hard += 1
            continue
        s = cand.get("match_score")
        if (
            emitted >= min_rows
            and floor is not None
            and (s is None or float(s) < float(floor))
        ):
            break
        if await emit_fn(cand, assessment):
            emitted += 1
    self._log_stage(
        "SampleMode",
        f"{stage_name}: pool={len(rows)} emitted={emitted} "
        f"hard_fails_skipped={skipped_hard} cap={sample_cap} min={min_rows} floor={floor}",
    )
    return emitted


async def emit_jobdiva_scored(cand, assessment, qualified_counter_key=None, min_score=None, as_full_row=False, *, build_screening, criteria, dedup_owner, finalize_candidate, jobagent_matched_ids, queue, seen_ids, self, summary):
    """Stage 3 of progressive JobDiva flow: score the (now-enriched)
            candidate and emit a ``candidate_detail`` patch with the scored
            payload. Mirrors :py:func:`emit_candidate` for dedup +
            non-US drop semantics, but emits a patch (the row already
            exists from emit_jobdiva_agent_result) instead of a fresh
            ``candidate`` event.

            ``min_score``: when set, rows scoring strictly below it are
            removed via a ``dropped`` patch instead of shown. Used for
            JobDiva-TalentSearch (machine-generated query → only surface
            >JOBDIVA_TALENTSEARCH_MIN_SCORE matches). Unscoreable rows
            (``detail_failed`` → match_score None) are always kept.

            ``as_full_row``: sample-mode variant. The agent_result skeleton
            was never emitted, so instead of patching a (nonexistent) row this
            emits a complete ``candidate`` event on success and stays silent on
            drops — no drop patch is sent for a row the client never saw.
            """
    cid = str(cand.get("candidate_id") or cand.get("id") or "")
    deferred_initial = bool(cand.pop("_defer_initial_emit", False))
    emit_full_row = as_full_row or deferred_initial
    location_reason = assessment.get("location_failure_reason") if isinstance(assessment, dict) else None
    # A selected experience range is an explicit recruiter constraint,
    # unlike the normal JobDiva relevance gate (which intentionally
    # remains advisory).  Do not let JOBDIVA_BYPASS_PASS_GATE leak an
    # enriched candidate outside that range back into Step 5.
    range_failure = self._experience_range_failure(criteria, assessment)
    if range_failure:
        self._log_stage(
            "ExperienceGate",
            f"dropping candidate_id={cid} outside configured experience range",
        )
        if not emit_full_row:
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": cid,
                "stage": "dropped",
                "patch": {"_stage": "dropped", "_drop_reason": "experience_years_out_of_range"},
            })
        return False
    if not assessment.get("passes") and location_reason in {"non_us_candidate"}:
        distance = cand.get("distance_miles")
        self._log_stage(
            "LocationGate",
            f"dropping candidate_id={cid} reason={location_reason} distance={distance}",
        )
        if not emit_full_row:
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": cid,
                "stage": "dropped",
                "patch": {"_stage": "dropped", "_drop_reason": "non_us_candidate"},
            })
        return False

    # Résumé enrichment (_apply_resume_location) can overwrite
    # cand["location"] with a value the first-paint gate in
    # emit_jobdiva_agent_result never saw — re-check here so a
    # résumé-recovered location outside every configured radius
    # still gets dropped instead of reaching Step 5.
    self._hydrate_candidate_location_from_zip(cand)
    scored_location_veto = self._location_hard_gate(cand, criteria)
    if scored_location_veto:
        summary["location_mismatch_dropped"] = summary.get("location_mismatch_dropped", 0) + 1
        self._log_stage(
            "LocationGate",
            f"dropping candidate_id={cid} reason={cand.get('location_veto_reason') or scored_location_veto} "
            f"distance={cand.get('distance_miles')}",
        )
        if not emit_full_row:
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": cid,
                "stage": "dropped",
                "patch": {"_stage": "dropped", "_drop_reason": "location_mismatch"},
            })
        return False

    cand["screening_summary"] = build_screening(assessment)

    if embedding_skill_match_for_family(self._current_family):
        try:
            cand_terms = self._candidate_skill_terms(cand)
            if cand_terms:
                await skill_embeddings.warm_terms(cand_terms)
        except Exception as exc:
            logger.warning(f"candidate-skill embedding warm failed: {exc}")

    cand = finalize_candidate(cand)

    # Min-score gate (TalentSearch only). Runs after finalize so the
    # score already includes the source-tier bonus / title boost.
    # Unscoreable rows (detail_failed → None) stay visible as
    # "Limited data" — we can't fairly judge them.
    if (
        min_score is not None
        and cand.get("match_score") is not None
        and int(cand.get("match_score") or 0) < int(min_score)
        and cid not in jobagent_matched_ids
    ):
        self._log_stage(
            "ScoreGate",
            f"dropping candidate_id={cid} source={cand.get('source')} "
            f"match_score={cand.get('match_score')} < min_score={min_score}",
        )
        # Release the id: the JobAgent pool shares seen_ids, and its
        # copy of this person may have been suppressed as a duplicate
        # when this row painted first. Freeing the id lets the
        # never-dropped JobAgent copy re-emit if it arrives later.
        if cid:
            seen_ids.discard(cid)
        if not emit_full_row:
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": cid,
                "stage": "dropped",
                "patch": {"_stage": "dropped", "_drop_reason": "below_min_score"},
            })
        return False

    # Cross-source dedup runs here (not at agent_result) since the
    # keys depend on email / phone / linkedin URL, which are only
    # reliably populated after enrichment ("the candidate details API").
    # POLICY: a JobDiva row, once shown, is never the dropped side of a
    # JobDiva-vs-other collision — it takes over the survivor slot and
    # absorbs the other source's best fields. Genuine intra-JobDiva
    # duplicates (Applicants vs Talent) merge into one surviving row.
    cross_keys = self._dedup_keys(cand)
    owner = next((dedup_owner[k] for k in cross_keys if k in dedup_owner), None)
    if owner is not None:
        owner_id = str(owner.get("candidate_id") or owner.get("id") or "")
        if not self._cand_is_jobdiva(owner):
            # JobDiva takes over from the non-JobDiva owner: absorb the
            # owner's best fields, remove the (already-shown) owner row,
            # and re-point the dedup keys to this JobDiva candidate.
            self._merge_candidate_best_of(cand, owner)
            for k in [k for k, v in dedup_owner.items() if v is owner]:
                del dedup_owner[k]
            for k in cross_keys:
                dedup_owner[k] = cand
            self._log_stage(
                "Dedup",
                f"cross_source merge: kept={cid}(JobDiva) "
                f"dropped={owner_id}({owner.get('source')}) keys={cross_keys}",
            )
            await queue.put({
                "type": "candidate_detail",
                "candidate_id": owner_id,
                "stage": "dropped",
                "patch": {"_stage": "dropped", "_drop_reason": "merged_into_jobdiva"},
            })
            # fall through: emit this JobDiva candidate's scored patch
        else:
            # Owner is also JobDiva (e.g. Applicants vs Talent for the
            # same person): a genuine intra-JobDiva duplicate. Merge
            # best-of into the surviving JobDiva row and drop this one.
            changed = self._merge_candidate_best_of(owner, cand)
            self._log_stage(
                "Dedup",
                f"cross_source merge: kept={owner_id}(JobDiva) "
                f"dropped={cid}(JobDiva) keys={cross_keys}",
            )
            if changed and owner_id:
                await queue.put({
                    "type": "candidate_detail",
                    "candidate_id": owner_id,
                    "patch": dict(changed),
                })
            if not emit_full_row:
                await queue.put({
                    "type": "candidate_detail",
                    "candidate_id": cid,
                    "stage": "dropped",
                    "patch": {"_stage": "dropped", "_drop_reason": "cross_source_duplicate"},
                })
            return False
    else:
        for k in cross_keys:
            dedup_owner[k] = cand

    if qualified_counter_key and assessment["passes"]:
        summary[qualified_counter_key] += 1

    if emit_full_row:
        # Sample mode: no skeleton row exists client-side — emit the
        # fully-enriched, scored candidate as a complete row. The
        # total_candidates counter normally increments at the
        # agent_result emission, which this path skipped.
        summary["total_candidates"] += 1
        await queue.put({"type": "candidate", "data": cand})
        return True

    scored_patch: Dict[str, Any] = {"_stage": "scored"}
    for key in (
        "match_score", "matched_skills", "missing_skills",
        "explainability", "match_score_details", "screening_summary",
        "enhanced_info", "enhanced_info_status", "education",
        "certifications", "skills", "urls", "experience_years",
        "name", "title", "location", "email", "phone",
        # Forward merged identity so a take-over reflects the unioned
        # sources + absorbed contact/profile on the surviving row, and
        # where an absorbed email came from (email_source: Launch
        # PAIR's getCandidateSeedEmail never seeds another source's).
        "sources", "profile_url", "linkedin_url", "image_url",
        "resume_id", "city", "state", "email_source",
        # Geo verdict → UI "~N mi away" badge on the location cell.
        "zipcode", "distance_miles", "location_out_of_radius",
        "location_match_reason",
        # Résumé-is-final residence: which side won and, when the
        # résumé overrode the profile, what the profile said → UI
        # "résumé" badge + tooltip on the location cell.
        "location_source", "profile_location", "location_conflict",
        "qualifications", "employee_status", "available",
        "availability_status", "current_company",
        # Candidate-details failure flag → UI renders "N/A" + keeps the
        # row launchable (vs. a 0% drop).
        "detail_failed",
        # "high_level" for JobDiva-JobAgent rows (LLM skills-match
        # skipped) → popup labels the score "JobDiva agent search".
        "scoring_mode",
        # No-contact company flag → UI greys the row out and disables
        # every action. False rides along too (v is not None) so a
        # stale flag clears client-side.
        "no_contact", "no_contact_reason", "no_contact_company",
    ):
        v = cand.get(key)
        if v is not None:
            scored_patch[key] = v
    await queue.put({
        "type": "candidate_detail",
        "candidate_id": cid,
        "stage": "scored",
        "patch": scored_patch,
    })
    return True
