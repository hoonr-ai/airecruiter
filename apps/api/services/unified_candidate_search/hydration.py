"""UnifiedCandidateSearch methods: hydration."""
import asyncio
from typing import List, Dict, Any, Optional
from services import jobdiva_rate_limit as _bi_rate_limit
from services import jobdiva_activity as _jobdiva_activity
from services.location import sanitize_candidate_location
from .common import _search_job_id, logger


class HydrationMixin:


    async def _hydrate_jobdiva_in_background(
        self,
        candidates: List[Dict[str, Any]],
        queue: asyncio.Queue,
        sentinel: Any,
        job_id: Optional[str] = None,
    ) -> None:
        """Page through CandidatesDetail for the top-scored JobDiva candidates
        and emit one ``candidate_detail`` event per hydrated row.

        Paced to respect JobDiva's rate limit:
        - Pages run serially (size = FAST_PATH_DETAIL_BACKGROUND_PAGE_SIZE).
        - Within a page we let ``_fetch_candidate_details_batch`` use its
          existing batching, which is one request per chunk.
        - On exception we exponential-backoff (5s → 60s cap) and continue
          to the next page rather than failing the whole search.
        - A short sleep between pages spreads the load.

        Emits ``sentinel`` on the queue exactly once when done so the
        orchestrator's drain loop terminates cleanly.
        """
        try:
            from core import sourcing_config as sc
            from services.jobdiva import get_field, _select_better_phone

            page_size = max(1, int(sc.FAST_PATH_DETAIL_BACKGROUND_PAGE_SIZE))
            page_delay = max(0.0, float(sc.FAST_PATH_DETAIL_BACKGROUND_PAGE_DELAY_S))
            if job_id is None:
                job_id = _search_job_id.get()

            # Snapshot each candidate's current phone so the détail patch can be
            # upgrade-only (never downgrade a mobile to a home/work number).
            existing_phone_by_id = {
                str(c.get("candidate_id") or c.get("id") or ""): str(c.get("phone") or "")
                for c in candidates
                if c.get("candidate_id") or c.get("id")
            }

            token = await self.jobdiva_service.authenticate()
            if not token:
                logger.warning(
                    "Hydration skipped: JobDiva authentication failed."
                )
                return

            backoff_s = 5.0
            total_pages = (len(candidates) + page_size - 1) // page_size

            def _page_ids(idx: int) -> List[str]:
                page = candidates[idx * page_size : (idx + 1) * page_size]
                return [
                    str(c.get("candidate_id") or c.get("id") or "")
                    for c in page
                    if c.get("candidate_id") or c.get("id")
                ]

            async def _fetch_page(ids: List[str]):
                # Yield to OTHER jobs' live searches/launches (jd:active:*).
                # Our own job's marker is ignored: the search that set it is
                # the one that spawned this hydration.
                await _jobdiva_activity.wait_while_busy(
                    job_id, include_self=False, label="hydration"
                )
                # Background: yields to live searches at the shared
                # JobDiva BI limiter (services/jobdiva_rate_limit).
                d = await self.jobdiva_service._fetch_candidate_details_batch(
                    token, ids, priority=_bi_rate_limit.BACKGROUND
                )
                n = await self.jobdiva_service._fetch_candidate_notes_action_types_batch(
                    token, ids, priority=_bi_rate_limit.BACKGROUND
                )
                q = await self.jobdiva_service._fetch_candidate_qualifications_batch(
                    token, ids, priority=_bi_rate_limit.BACKGROUND
                )
                return d, n, q

            # Page k+1 is fetched while page k's patches are built/emitted.
            prefetch: Optional[asyncio.Task] = None
            for page_idx in range(total_pages):
                page_ids = _page_ids(page_idx)
                if not page_ids:
                    continue

                try:
                    if prefetch is not None:
                        task, prefetch = prefetch, None
                        detail_map, notes_actions_map, quals_map = await task
                    else:
                        detail_map, notes_actions_map, quals_map = await _fetch_page(page_ids)
                    # Reset backoff on success.
                    backoff_s = 5.0
                    next_ids = _page_ids(page_idx + 1) if page_idx + 1 < total_pages else []
                    if next_ids and page_delay <= 0:
                        prefetch = asyncio.create_task(_fetch_page(next_ids))
                except Exception as exc:
                    logger.warning(
                        "Hydration page %d/%d failed: %s — backing off %.1fs",
                        page_idx + 1, total_pages, exc, backoff_s,
                    )
                    try:
                        await asyncio.sleep(backoff_s)
                    except asyncio.CancelledError:
                        raise
                    backoff_s = min(60.0, backoff_s * 2.0)
                    continue

                # Convert each detail record into a patch and emit.
                hydrated = 0
                for cand_id, detail in (detail_map or {}).items():
                    if not detail:
                        continue
                    patch: Dict[str, Any] = {}
                    email = (get_field(detail, [
                        "email", "EMAIL", "emailAddress", "EMAILADDRESS",
                        "emails", "EMAILS", "emailId", "EMAILID",
                        "email1", "EMAIL1", "email2", "EMAIL2",
                        "alternateEmail", "ALTERNATEEMAIL",
                    ]) or "")
                    # Upgrade-only phone: pick the détail's best (mobile-typed)
                    # number and only patch it when it beats what we already
                    # have — never downgrade a JobAgent mobile to a home/work #.
                    better_phone = _select_better_phone(
                        existing_phone_by_id.get(str(cand_id), ""), detail
                    )
                    address1 = (get_field(detail, ["address1", "ADDRESS1", "address", "ADDRESS"]) or "")
                    linkedin = (get_field(detail, ["linkedinUrl", "LINKEDINURL", "linkedin", "LINKEDIN", "linkedIn", "LINKEDIN_URL"]) or "")
                    resume_id = (get_field(detail, ["resumeId", "RESUMEID", "resume_id"]) or "")
                    resume_text = self.jobdiva_service._extract_resume_text(detail) or ""
                    city = (get_field(detail, ["city", "CITY", "locationCity", "LOCATIONCITY"]) or "")
                    state = (get_field(detail, ["state", "STATE", "locationState", "LOCATIONSTATE"]) or "")
                    if email:
                        patch["email"] = str(email).strip()
                    if better_phone and better_phone != existing_phone_by_id.get(str(cand_id), "").strip():
                        patch["phone"] = better_phone
                    if address1:
                        patch["address1"] = str(address1).strip()
                    if linkedin:
                        patch["linkedin_url"] = str(linkedin).strip()
                    if resume_id:
                        patch["resume_id"] = str(resume_id).strip()
                    if resume_text:
                        patch["resume_text"] = resume_text
                        patch["abstract"] = resume_text[:240].replace("\n", " ").strip()
                    if city:
                        patch["city"] = str(city).strip()
                    if state:
                        patch["state"] = str(state).strip()
                    # Rebuild the display string too — the UI reads `location`
                    # first, so patching only city/state leaves a stale (or
                    # LLM-corrupted) `location` on screen forever. Sanitized:
                    # CRM city fields can literally say "REMOTE", and this
                    # patch bypasses the finalize_candidate choke-point.
                    if city or state:
                        rebuilt_loc = sanitize_candidate_location(", ".join(
                            p for p in [str(city).strip(), str(state).strip()] if p
                        ))
                        if rebuilt_loc:
                            patch["location"] = rebuilt_loc

                    quals = detail.get("qualifications") or detail.get("QUALIFICATIONS") or quals_map.get(str(cand_id)) or []
                    if quals:
                        patch["qualifications"] = quals
                        for q in quals:
                            if not isinstance(q, dict): continue
                            qval = str(q.get("QUALIFICATIONVALUE") or q.get("qualificationValue") or "").strip()
                            qname = str(q.get("QUALIFICATION") or q.get("qualificationName") or "").strip()
                            if "current employee" in qval.lower() or "current employee" in qname.lower():
                                patch["employee_status"] = "Current Employee"
                    emp_status = get_field(detail, ["EMPLOYEESTATUS", "employeeStatus", "CURRENTEMPLOYEE", "currentEmployee", "ASSIGNMENTSTATUS", "assignmentStatus"])
                    if emp_status:
                        patch["employee_status"] = str(emp_status).strip()
                    avail_val = get_field(detail, ["AVAILABLE", "available", "STATUS", "status"])
                    if avail_val is not None and avail_val != "":
                        patch["available"] = avail_val

                    action_types = notes_actions_map.get(str(cand_id), [])
                    if action_types:
                        patch["action_types"] = action_types
                        for at in action_types:
                            at_low = at.lower()
                            if "offer accepted" in at_low or "placed" in at_low or "hired" in at_low:
                                patch["offer_status"] = "Offer Accepted"
                                break
                            elif "offer extended" in at_low:
                                patch["offer_status"] = "Offer Extended"

                    if not patch:
                        continue

                    await queue.put({
                        "type": "candidate_detail",
                        "candidate_id": str(cand_id),
                        "patch": patch,
                    })
                    hydrated += 1

                logger.info(
                    "Hydration page %d/%d: %d patches for %d ids",
                    page_idx + 1, total_pages, hydrated, len(page_ids),
                )

                if page_delay > 0 and page_idx + 1 < total_pages:
                    try:
                        await asyncio.sleep(page_delay)
                    except asyncio.CancelledError:
                        raise
        except asyncio.CancelledError:
            # Caller closed the generator. Let the SENTINEL still fire in finally.
            raise
        except Exception as exc:
            logger.exception("Hydration task failed: %s", exc)
        finally:
            _pf = locals().get("prefetch")
            if _pf is not None and not _pf.done():
                _pf.cancel()
            try:
                await queue.put(sentinel)
            except Exception:
                pass
