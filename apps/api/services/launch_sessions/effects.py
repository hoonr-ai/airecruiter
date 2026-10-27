"""Everything a launch session does OUTSIDE its own tables, behind one seam.

The stages call these and nothing else for search, contact lookups, saves,
Pairbot, JobDiva provisioning, emails and timing stamps, so the runner can be
tested with fakes and the production calls stay the exact code the browser
flow uses (Units D1 / D2 / C — never re-implemented here):

  search_events        routers.candidates.iter_search_events
  enrich               routers.candidates.enrich_contacts_for_launch
  save                 routers.candidates.save_candidates (direct call)
  run_launch           routers.engagement.run_launch
  claim_for_session /  routers.engagement.claim_candidates_for_session /
  release_claims         release_session_claims
  reconcile            routers.engagement.reconcile_with_pairbot
  provision            routers.engagement.provision_sent_candidates
  fire_side_effects    routers.engagement.fire_launch_side_effects
  register_launch_run  routers.engagement.register_session_launch_run
  stamp_* / timing     services.launch_timing

Router modules are imported lazily: importing the runner never pulls them in.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, Sequence, Set


class Effects:
    # ── sourcing ────────────────────────────────────────────────────────────
    def build_search_request(self, payload: Dict[str, Any]) -> Any:
        from models import CandidateSearchRequest

        return CandidateSearchRequest(**(payload or {}))

    def search_events(
        self, request: Any, *, schedule_cross_submissions: bool, defer_paid_contact: bool = False,
    ) -> AsyncIterator[Dict[str, Any]]:
        from routers import candidates

        return candidates.iter_search_events(
            request, schedule_cross_submissions=schedule_cross_submissions, defer_paid_contact=defer_paid_contact,
        )

    # ── contacts ────────────────────────────────────────────────────────────
    def enrich(self, items: Sequence[Dict[str, Any]], *, job_id: Any, concurrency: int) -> AsyncIterator[Dict[str, Any]]:
        from routers import candidates

        return candidates.enrich_contacts_for_launch(list(items), job_id=job_id, concurrency=concurrency)

    def dnc_phones(self) -> Set[str]:
        from services.dnc_storage import load_dnc_phone_set

        return set(load_dnc_phone_set() or ())

    # ── save ────────────────────────────────────────────────────────────────
    def resolve_user(self, email: str) -> Any:
        from core.auth import resolve_user_identity

        return resolve_user_identity(email)

    async def save(self, jobdiva_id: str, rows: Sequence[Dict[str, Any]], user: Any) -> Dict[str, Any]:
        from models import CandidateSaveRecord, CandidatesSaveRequest
        from routers import candidates

        request = CandidatesSaveRequest(
            jobdiva_id=str(jobdiva_id), candidates=[CandidateSaveRecord(**r) for r in rows],
        )
        return await candidates.save_candidates(request, user=user)

    async def recruiter_email_gate(self, jobdiva_id: str) -> None:
        from routers import candidates

        await candidates._enforce_recruiter_email_gate(str(jobdiva_id))

    # ── launch ──────────────────────────────────────────────────────────────
    def launch_request(self, **fields: Any) -> Any:
        from routers.engagement import LaunchRequest

        return LaunchRequest(**fields)

    def run_launch(self, request: Any, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        from routers import engagement

        return engagement.run_launch(request, **kwargs)

    def register_launch_run(self, launch_id: str, job_id: str, *, requested_by: Optional[str], session_id: str) -> None:
        from routers import engagement

        engagement.register_session_launch_run(
            launch_id, job_id, requested_by=requested_by, launch_session_id=session_id,
        )

    def claim_for_session(
        self, job_keys: Sequence[str], ids: Sequence[str], session_id: str,
        duplicates: Optional[Dict[str, Any]] = None,
    ) -> list:
        """``duplicates`` (optional out-param) receives the person gate's
        ``{id: (holder id, key kind)}``: ids not claimed because another id of
        the same person is already held for the job."""
        from routers import engagement

        return engagement.claim_candidates_for_session(
            tuple(job_keys), list(ids), session_id, duplicates=duplicates,
        )

    def release_claims(self, session_id: str, job_keys: Sequence[str], ids: Sequence[str]) -> int:
        """Only the given ids — never "every queued row of the session": a
        posted row the core put back to 'queued' must not be released
        (launching.hand_back_claims parks those instead)."""
        from routers import engagement

        return engagement.release_session_claims(session_id, tuple(job_keys), list(ids))

    async def reconcile(
        self, job_keys: Sequence[str], ids: Sequence[str], *, session_id: str, own_bulk_ids: Sequence[str],
    ) -> Dict[str, Any]:
        from routers import engagement

        return await engagement.reconcile_with_pairbot(
            list(job_keys), list(ids), session_id=session_id, own_bulk_ids=list(own_bulk_ids),
        )

    async def settle_parked_legacy_rows(self) -> Dict[str, int]:
        from routers import engagement

        return await engagement.settle_parked_legacy_rows()

    # ── provisioning / side effects ────────────────────────────────────────
    def provisioning_enabled(self) -> bool:
        from routers import engagement

        return engagement.jobdiva_provisioning_enabled()

    async def provision(self, job_id: str, ids: Sequence[str]) -> Dict[str, Any]:
        from routers import engagement

        return await engagement.provision_sent_candidates(job_id, list(ids))

    def fire_side_effects(self, job_id: str, **kwargs: Any) -> bool:
        from routers import engagement

        return engagement.fire_launch_side_effects(job_id, **kwargs)

    # ── timings ─────────────────────────────────────────────────────────────
    def stamp_launch_clicked(self, job_id: str, at: Any) -> Optional[str]:
        from services import launch_timing

        return launch_timing.stamp_launch_clicked(job_id, at=at)

    def stamp_first_launch(self, job_key: str) -> Optional[str]:
        from services import launch_timing

        return launch_timing.stamp_first_launch(job_key)

    def record_timing_event(self, row: Dict[str, Any], *, job_first_launch_at: Any) -> Any:
        from services import launch_timing

        return launch_timing.record_timing_event(row, job_first_launch_at=job_first_launch_at)
