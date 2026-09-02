"""Launch-time employer resolution for JobDiva candidates.

Policy (2026-09-02): whenever a candidate's employer data is missing or not
confident enough, fetch their resume and parse it — every time, before PAIR
outreach. JobDiva itself exposes no structured employer field anywhere in the
sourcing flow (live-probed: TalentSearch rows and CandidatesDetail carry
none), so the resume is the primary source and everything else corroborates.

The resolution ladder, per candidate entering a launch:

  1. Stored extraction — `candidate_enhanced_info.company_experience`
     (hydrated by the launch gate already). Non-empty ⇒ confident, done.
  2. Resume fetch + LLM parse — the stored `sourced_candidates.resume_text`
     when real, else fetched from JobDiva; parsed via the existing
     `process_jobdiva_candidate` pipeline (resume-hash cached, persists to
     candidate_enhanced_info so the next launch starts at step 1).
  3. JobDiva profile work history — `CandidatesProfileDetail.EXPERIENCE`:
     JobDiva's own resume parse, free-text lines with structured date
     ranges. Noisy (fragments like "India" appear) and sparse (~27% of a
     probed sample), so it is attached as `jobdiva_profile_experience` for
     the gate's one-directional text matching — corroboration and fallback,
     never a primary source.

Candidates that finish the ladder with nothing are NOT blocked (that would
hold back every no-resume, phone-only candidate forever) — they are reported
as employer-unverified by `employer_verification_state` so launches surface
them instead of passing them silently.

Every failure here fails OPEN toward the pre-resolution behavior: resolution
must never take a launch down, only inform it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── JobDiva profile EXPERIENCE parsing ─────────────────────────────────────
#
# Observed shape (probe 2026-09-02):
#   [{"DATE": "08/2023 - 11/2024", "DETAILS": "Data Engineer | Walmart, …",
#     "DBID": "7"}, …]
# DATE is reliably "MM/YYYY - MM/YYYY" (assume an absent/`present` right side
# means an open, current engagement); DETAILS is whatever resume line the
# parser latched onto — sometimes the employer header, sometimes a
# description fragment.

_MMYYYY_RE = re.compile(r"(\d{1,2})\s*/\s*((?:19|20)\d{2})")
_OPEN_END_RE = re.compile(r"present|current|till\s*date|to\s*date|now", re.I)


def _parse_month_year(raw: str) -> Optional[Tuple[int, int]]:
    m = _MMYYYY_RE.search(str(raw or ""))
    if not m:
        return None
    month = int(m.group(1))
    if not 1 <= month <= 12:
        month = 0
    return (int(m.group(2)), month)


def parse_profile_experience(record: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """CandidatesProfileDetail record → [{"text", "start", "end", "is_current"}].

    `end` is a (year, month) tuple or None; `is_current` is True only for an
    open-ended range (no end date, or a present/current marker) — a range that
    merely ended recently is the LAST engagement, not a current one.
    Entries with no DETAILS text are dropped: there is nothing to match on.
    """
    entries: List[Dict[str, Any]] = []
    for exp in (record or {}).get("EXPERIENCE") or []:
        if not isinstance(exp, dict):
            continue
        text = str(exp.get("DETAILS") or "").strip()
        if not text:
            continue
        date_raw = str(exp.get("DATE") or "").strip()
        parts = re.split(r"\s*[-–—]\s*", date_raw, maxsplit=1)
        start = _parse_month_year(parts[0]) if parts else None
        end_raw = parts[1] if len(parts) > 1 else ""
        end = _parse_month_year(end_raw)
        is_current = bool(date_raw) and end is None and (
            not end_raw.strip() or bool(_OPEN_END_RE.search(end_raw))
        )
        entries.append({
            "text": text,
            "start": start,
            "end": end,
            "is_current": is_current,
        })
    return entries


def profile_current_and_last_texts(
    entries: List[Dict[str, Any]]
) -> Tuple[List[str], List[str]]:
    """Split parsed profile entries into (current texts, last-employer texts),
    mirroring company_match's current-then-last ladder: `last` is only the
    single most recent ended entry (by parsed end date, falling back to list
    order, which JobDiva emits reverse-chronologically)."""
    current = [e["text"] for e in entries if e.get("is_current")]
    past = [e for e in entries if not e.get("is_current")]
    last: List[str] = []
    if past:
        dated = [e for e in past if e.get("end")]
        top = max(dated, key=lambda e: e["end"]) if dated else past[0]
        last = [top["text"]]
    return current, last


def candidate_profile_texts(candidate: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(current, last) profile texts off a candidate dict, tolerant of the
    entries living at the top level or inside the `data` blob."""
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    entries = candidate.get("jobdiva_profile_experience") or data.get("jobdiva_profile_experience") or []
    if not isinstance(entries, list):
        return [], []
    return profile_current_and_last_texts([e for e in entries if isinstance(e, dict)])


# ── confidence + verification state ───────────────────────────────────────

def has_confident_employer_signal(candidate: Dict[str, Any]) -> bool:
    """True when the candidate carries employer data we trust enough to skip
    the resume pass: a non-empty extracted `company_experience`, or an
    explicit flat `current_company` (nothing stamps that today, but a future
    recruiter override lands there and must win). A headline parse alone is
    NOT confident — per the standing policy, anything less than parsed
    employment history means fetch the resume and parse it."""
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    enhanced = data.get("enhanced_info") if isinstance(data.get("enhanced_info"), dict) else {}
    for exp_list in (data.get("company_experience"), enhanced.get("company_experience")):
        if isinstance(exp_list, list):
            for exp in exp_list:
                if isinstance(exp, dict) and str(
                    exp.get("company") or exp.get("company_name")
                    or exp.get("employer") or exp.get("name") or ""
                ).strip():
                    return True
    for flat in (data.get("current_company"), enhanced.get("current_company")):
        if flat and str(flat).strip():
            return True
    return False


def employer_verification_state(candidate: Dict[str, Any]) -> str:
    """How well we know this candidate's employer, after resolution:

      'verified'     — structured current/last-employer signals exist (the
                       gate judged real company names);
      'profile_only' — only JobDiva's noisy profile lines exist (the gate
                       ran its text fallback over them);
      'unverified'   — nothing at all; the client/no-contact checks had
                       nothing to judge. Passing the gate in this state means
                       UNKNOWN, not clean.
    """
    try:
        from services.company_match import collect_current_companies, collect_last_companies
        if collect_current_companies(candidate) or collect_last_companies(candidate):
            return "verified"
    except Exception:  # noqa: BLE001 — classification must never raise
        pass
    cur, last = candidate_profile_texts(candidate)
    if cur or last:
        return "profile_only"
    return "unverified"


# ── the resolution pass ───────────────────────────────────────────────────

def _is_jobdiva_row(candidate: Dict[str, Any]) -> bool:
    """Rows whose candidate_id is a JobDiva person id — the only ones the
    JobDiva resume/profile endpoints can resolve. External sources (Unipile
    hashes, Exa URLs) keep their existing signals untouched."""
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    source = str(candidate.get("source") or data.get("source") or "")
    if "jobdiva" in source.lower():
        return True
    return str(candidate.get("candidate_id") or "").strip().isdigit()


def _stored_resume_text(candidate: Dict[str, Any]) -> str:
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    return str(candidate.get("resume_text") or data.get("resume_text") or "")


async def resolve_employer_signals(
    candidates: List[Dict[str, Any]],
    *,
    service: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Resume-first employer resolution for every candidate that needs it.

    Takes launch-hydrated candidate dicts (post `_merge_employer_signals`) and
    returns `{candidate_id: signals}` where `signals` is shaped for another
    `_merge_employer_signals` pass: `company_experience` / `title` from a
    fresh resume parse, `jobdiva_profile_experience` from JobDiva's profile,
    and an `employer_resolution` breadcrumb saying what happened.

    Never raises; a candidate that can't be resolved simply comes back
    without new signals (and will surface as employer-unverified).
    """
    try:
        from core import sourcing_config as _sc
    except Exception:  # noqa: BLE001
        _sc = None
    if _sc is not None and not getattr(_sc, "EMPLOYER_RESOLUTION_ENABLED", True):
        return {}

    targets: List[Tuple[str, Dict[str, Any]]] = []
    seen: set = set()
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        cid = str(cand.get("candidate_id") or "").strip()
        if not cid or cid in seen:
            continue
        if not _is_jobdiva_row(cand) or has_confident_employer_signal(cand):
            continue
        seen.add(cid)
        targets.append((cid, cand))
    if not targets:
        return {}

    max_candidates = int(getattr(_sc, "EMPLOYER_RESOLUTION_MAX_CANDIDATES", 300) or 300) if _sc else 300
    if len(targets) > max_candidates:
        logger.warning(
            "employer_resolution capped: resolving %d of %d candidates "
            "(EMPLOYER_RESOLUTION_MAX_CANDIDATES) — the rest launch on stored signals only",
            max_candidates, len(targets),
        )
        targets = targets[:max_candidates]

    if service is None:
        from services.jobdiva import jobdiva_service as service  # noqa: PLC0415

    out: Dict[str, Dict[str, Any]] = {
        cid: {"employer_resolution": {"attempted": True, "extraction": "not_run", "profile_entries": 0}}
        for cid, _ in targets
    }

    # 1. JobDiva profile work history for everyone in one batched call —
    #    corroboration for parsed resumes, the only signal for no-resume rows.
    try:
        profiles = await service.fetch_candidate_profiles_batch([cid for cid, _ in targets])
    except Exception as exc:  # noqa: BLE001
        logger.warning("employer_resolution: profile fetch failed (continuing): %s", exc)
        profiles = {}
    for cid, _ in targets:
        entries = parse_profile_experience(profiles.get(cid))
        if entries:
            out[cid]["jobdiva_profile_experience"] = entries
        out[cid]["employer_resolution"]["profile_entries"] = len(entries)

    # 2. Resume text: stored row first, JobDiva fetch for the rest.
    try:
        from services.sourced_candidates_storage import _has_real_resume_text
    except Exception:  # noqa: BLE001
        def _has_real_resume_text(text: str) -> bool:  # type: ignore[misc]
            return bool(str(text or "").strip())

    resume_texts: Dict[str, str] = {}
    need_fetch: List[str] = []
    for cid, cand in targets:
        stored = _stored_resume_text(cand)
        if _has_real_resume_text(stored):
            resume_texts[cid] = stored
        else:
            need_fetch.append(cid)
    if need_fetch:
        try:
            fetched = await service.fetch_resume_texts(need_fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("employer_resolution: resume fetch failed for %d candidate(s): %s", len(need_fetch), exc)
            fetched = {}
        for cid, text in (fetched or {}).items():
            if _has_real_resume_text(text):
                resume_texts[str(cid)] = text

    # 3. Parse each resume through the existing extraction pipeline. The
    #    resume-hash cache makes repeat candidates free; fresh parses are
    #    bounded by the semaphore, a per-candidate timeout, and an overall
    #    budget after which stragglers are skipped rather than holding the
    #    launch — persistence means they're stored signals next time anyway.
    concurrency = int(getattr(_sc, "EMPLOYER_RESOLUTION_CONCURRENCY", 6) or 6) if _sc else 6
    per_timeout = float(getattr(_sc, "EMPLOYER_RESOLUTION_PER_CANDIDATE_TIMEOUT_S", 45.0) or 45.0) if _sc else 45.0
    budget = float(getattr(_sc, "EMPLOYER_RESOLUTION_BUDGET_S", 180.0) or 180.0) if _sc else 180.0
    sem = asyncio.Semaphore(max(1, concurrency))
    started = time.monotonic()

    async def _extract(cid: str, cand: Dict[str, Any]) -> None:
        meta = out[cid]["employer_resolution"]
        text = resume_texts.get(cid)
        if not text:
            meta["extraction"] = "no_resume"
            return
        async with sem:
            if time.monotonic() - started > budget:
                meta["extraction"] = "budget_exhausted"
                return
            try:
                from services.sourced_candidates_storage import process_jobdiva_candidate  # noqa: PLC0415
                enhanced = await asyncio.wait_for(
                    process_jobdiva_candidate({
                        "candidate_id": cid,
                        "resume_text": text,
                        "name": cand.get("name"),
                        "email": cand.get("email"),
                        "phone": cand.get("phone"),
                        "title": cand.get("title") or cand.get("headline"),
                        "location": cand.get("location"),
                        "source": str(cand.get("source") or "JobDiva"),
                    }),
                    timeout=per_timeout,
                )
            except asyncio.TimeoutError:
                meta["extraction"] = "timeout"
                return
            except Exception as exc:  # noqa: BLE001
                meta["extraction"] = "error"
                logger.warning("employer_resolution: extraction failed for %s: %s", cid, exc)
                return
        if not isinstance(enhanced, dict):
            meta["extraction"] = "error"
            return
        if enhanced.get("skipped"):
            meta["extraction"] = "skipped_placeholder_resume"
            return
        meta["extraction"] = "error" if enhanced.get("_extraction_error") else "completed"
        company_exp = enhanced.get("company_experience") or []
        if isinstance(company_exp, list) and company_exp:
            out[cid]["company_experience"] = company_exp
        title = str(enhanced.get("current_title") or "").strip()
        if title:
            out[cid]["title"] = title

    await asyncio.gather(*[_extract(cid, cand) for cid, cand in targets])

    resolved = sum(1 for v in out.values() if v.get("company_experience"))
    profile_only = sum(
        1 for v in out.values()
        if not v.get("company_experience") and v.get("jobdiva_profile_experience")
    )
    logger.info(
        "employer_resolution: %d candidate(s) lacked confident employer data — "
        "%d resolved from resume parse, %d have JobDiva profile lines only, %d still unknown "
        "(%.1fs)",
        len(targets), resolved, profile_only, len(targets) - resolved - profile_only,
        time.monotonic() - started,
    )
    return out
