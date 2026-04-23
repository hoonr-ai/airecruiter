"""Tira — the in-app recruiting sidekick.

Endpoints:

- POST /tira/match       Score a resume against an existing job's rubric.
- POST /tira/boolean     Generate a boolean search string from a JD text/file.
- POST /tira/bug-report  Email a bug report via SMTP.

Thin wrappers around mechanisms that already exist elsewhere in the codebase:
- Resume/JD text extraction: routers/manual_candidates._extract_resume_text_from_upload.
- Scoring: services/unified_candidate_search._score_candidate.
- Chat + tool-calling for status lookups: services/chat_service.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import psycopg2.extras
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from routers._helpers import get_db_connection
from routers.manual_candidates import _extract_resume_text_from_upload

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /tira/match — score a resume against a job rubric without persisting
# ---------------------------------------------------------------------------

@router.post("/tira/match")
async def tira_match_resume(
    job_id: str = Form(...),
    resume_file: UploadFile = File(...),
):
    """Score an uploaded resume against a saved job's rubric.

    Returns score + matched / missing skills + explainability, WITHOUT persisting
    anything to sourced_candidates. This is the on-demand "does this candidate
    look right for this job?" path, triggered from the Tira side panel.
    """
    from services.unified_candidate_search import SearchCriteria, unified_search_service
    from services.sourced_candidates_storage import process_jobdiva_candidate

    # Read + parse resume up front — fail fast if unreadable.
    content = await resume_file.read()
    resume_text = _extract_resume_text_from_upload(resume_file.filename or "", content)
    if not resume_text or len(resume_text.strip()) < 40:
        raise HTTPException(status_code=400, detail="Could not extract readable text from the resume. Try PDF, DOCX, or TXT.")

    # Load the job's rubric + sourcing filters.
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(
        "SELECT job_id, jobdiva_id, sourcing_filters, resume_match_filters, title "
        "FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
        (job_id, job_id),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job_row = dict(row)
    sourcing_filters = job_row.get("sourcing_filters") or {}
    resume_match_filters = job_row.get("resume_match_filters") or []
    if isinstance(sourcing_filters, str):
        try:
            sourcing_filters = json.loads(sourcing_filters)
        except Exception:
            sourcing_filters = {}
    if isinstance(resume_match_filters, str):
        try:
            resume_match_filters = json.loads(resume_match_filters)
        except Exception:
            resume_match_filters = []

    # Enrich the resume via the same LLM extractor used for real candidates.
    # This gives _score_candidate structured skills, title, years_of_experience
    # — dramatically improves scoring vs passing raw text.
    candidate: Dict[str, Any] = {
        "candidate_id": "tira_match_ephemeral",
        "provider_id": "tira_match_ephemeral",
        "jobdiva_id": job_row.get("jobdiva_id") or str(job_row.get("job_id")),
        "name": (resume_file.filename or "Candidate").rsplit(".", 1)[0],
        "email": "",
        "phone": "",
        "resume_text": resume_text,
        "source": "tira-match",
        "title": "",
        "location": "",
    }
    try:
        extraction = await process_jobdiva_candidate(candidate)
        enhanced = extraction.get("raw", {}) if isinstance(extraction, dict) else {}
    except Exception as enrich_err:
        logger.warning(f"Tira match: resume enrichment failed: {enrich_err}")
        enhanced = {}

    if enhanced:
        candidate["name"] = enhanced.get("candidate_name") or candidate["name"]
        candidate["email"] = enhanced.get("email") or candidate["email"]
        candidate["phone"] = enhanced.get("phone") or candidate["phone"]
        candidate["title"] = enhanced.get("job_title") or candidate["title"]
        candidate["location"] = enhanced.get("current_location") or candidate["location"]
        candidate["skills"] = enhanced.get("structured_skills") or enhanced.get("skills") or []
        candidate["years_of_experience"] = enhanced.get("years_of_experience")

    title_criteria = [c for c in resume_match_filters if c.get("type") == "title"] if isinstance(resume_match_filters, list) else []
    skill_criteria = [c for c in resume_match_filters if c.get("type") == "skill"] if isinstance(resume_match_filters, list) else []
    keywords = list(sourcing_filters.get("keywords") or []) if isinstance(sourcing_filters, dict) else []
    location_val = ""
    if isinstance(sourcing_filters, dict) and sourcing_filters.get("locations"):
        first_loc = sourcing_filters["locations"][0] if sourcing_filters["locations"] else None
        if isinstance(first_loc, dict):
            location_val = first_loc.get("value", "") or ""
        elif isinstance(first_loc, str):
            location_val = first_loc

    criteria = SearchCriteria(
        job_id=str(job_row.get("job_id")),
        title_criteria=title_criteria,
        skill_criteria=skill_criteria,
        keywords=keywords,
        resume_match_filters=resume_match_filters if isinstance(resume_match_filters, list) else [],
        location=location_val,
        sources=["tira-match"],
    )

    score_result = unified_search_service._score_candidate(candidate, criteria)

    return {
        "status": "success",
        "job": {
            "job_id": str(job_row.get("job_id")),
            "jobdiva_id": job_row.get("jobdiva_id"),
            "title": job_row.get("title"),
        },
        "candidate": {
            "name": candidate["name"],
            "email": candidate.get("email"),
            "phone": candidate.get("phone"),
            "title": candidate.get("title"),
            "location": candidate.get("location"),
            "years_of_experience": candidate.get("years_of_experience"),
            "skills": candidate.get("skills") or [],
        },
        "score": score_result.get("score", 0),
        "matched_skills": score_result.get("matched_skills", []),
        "missing_skills": score_result.get("missing_skills", []),
        "explainability": score_result.get("explainability", []),
        "match_score_details": score_result.get("score_details", {}),
    }


# ---------------------------------------------------------------------------
# /tira/boolean — generate a boolean search string from a JD (text or file)
# ---------------------------------------------------------------------------

_BOOLEAN_SYSTEM_PROMPT = (
    "You are a senior technical sourcer. Turn a job description into a single "
    "boolean search string suitable for JobDiva, LinkedIn Recruiter, or Dice.\n\n"
    "Rules:\n"
    "- Extract must-have titles (with realistic synonyms) and must-have skills.\n"
    "- Extract nice-to-have skills (separate).\n"
    "- Extract exclusions (intern, junior, student, contract-only, etc. — only if implied).\n"
    "- Quote multi-word phrases. Use OR for synonyms inside each group and AND between groups.\n"
    "- Form: (\"Title A\" OR \"Title B\") AND (skill1 OR skill2) AND (nice1 OR nice2) NOT (exclude1 OR exclude2).\n"
    "- If there are no nice-to-haves or no exclusions, omit that group.\n"
    "- Keep the final string under 600 characters; trim synonyms if needed.\n\n"
    "Respond with JSON matching exactly this schema:\n"
    "{\n"
    '  "boolean_string": string,\n'
    '  "must_have_titles": string[],\n'
    '  "must_have_skills": string[],\n'
    '  "nice_to_have": string[],\n'
    '  "exclusions": string[]\n'
    "}"
)


@router.post("/tira/boolean")
async def tira_boolean_from_jd(
    jd_text: str = Form(""),
    jd_file: Optional[UploadFile] = File(None),
):
    """Turn a job description into a boolean search string via gpt-4o-mini.

    Accepts either `jd_text` (pasted) or `jd_file` (PDF/DOCX/TXT). When both are
    provided, pasted text wins. If neither has any meaningful content, responds
    with 400.
    """
    from services.chat_service import chat_service  # lazy: avoids circular import at module load

    text = (jd_text or "").strip()
    if not text and jd_file is not None:
        content = await jd_file.read()
        text = _extract_resume_text_from_upload(jd_file.filename or "", content).strip()

    if len(text) < 40:
        raise HTTPException(
            status_code=400,
            detail="Paste a job description, or upload a readable PDF/DOCX/TXT.",
        )

    if chat_service.client is None:
        raise HTTPException(
            status_code=503,
            detail="OpenAI isn't configured on the server (missing OPENAI_API_KEY).",
        )

    # Truncate very long JDs — gpt-4o-mini handles 128k but JDs rarely need
    # more than a few thousand chars and it keeps latency predictable.
    MAX_CHARS = 12_000
    jd = text[:MAX_CHARS]

    try:
        response = await chat_service.client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _BOOLEAN_SYSTEM_PROMPT},
                {"role": "user", "content": f"Job description:\n\n{jd}"},
            ],
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Model returned non-JSON output.")
    except Exception as e:
        logger.error(f"Tira boolean: model call failed: {e}")
        raise HTTPException(status_code=502, detail=f"Model call failed: {e}")

    # Light validation — keep shapes predictable for the frontend.
    def _as_str_list(v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    boolean_string = str(parsed.get("boolean_string") or "").strip()
    if not boolean_string:
        raise HTTPException(status_code=502, detail="Model could not produce a boolean string.")

    return {
        "status": "success",
        "boolean_string": boolean_string,
        "must_have_titles": _as_str_list(parsed.get("must_have_titles")),
        "must_have_skills": _as_str_list(parsed.get("must_have_skills")),
        "nice_to_have": _as_str_list(parsed.get("nice_to_have")),
        "exclusions": _as_str_list(parsed.get("exclusions")),
        "source": "pasted" if (jd_text or "").strip() else "file",
    }


# ---------------------------------------------------------------------------
# /tira/bug-report — send an email to the maintainer over SMTP
# ---------------------------------------------------------------------------

def _send_bug_email(
    *,
    title: str,
    description: str,
    page_url: str,
    user_agent: str,
    screenshot_name: Optional[str],
    screenshot_bytes: Optional[bytes],
    screenshot_mime: Optional[str],
) -> Dict[str, Any]:
    """Build and send the bug-report email. Returns a status dict.

    If SMTP env vars are not configured, the payload is logged and the caller
    gets {"sent": False, "logged": True} so the UI can still show success.
    """
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "465") or 465)
    use_ssl = (os.getenv("SMTP_USE_SSL", "true").strip().lower() in ("1", "true", "yes", "y"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip() or user
    recipient = os.getenv("SMTP_TO", "").strip()

    if not (host and user and password and sender and recipient):
        logger.warning(
            "Tira bug report received but SMTP is not configured — logging instead.\n"
            f"Title: {title}\nFrom page: {page_url}\nUA: {user_agent}\n\n{description}"
        )
        return {"sent": False, "logged": True}

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = f"[Hoonr bug] {title[:120]}"
    body_lines = [
        description.strip() or "(no description provided)",
        "",
        "— Context —",
        f"Page URL: {page_url or '(unknown)'}",
        f"User-Agent: {user_agent or '(unknown)'}",
        f"Reported via: Tira bug-report form",
    ]
    msg.set_content("\n".join(body_lines))

    if screenshot_bytes and screenshot_name:
        maintype, _, subtype = (screenshot_mime or "image/png").partition("/")
        maintype = maintype or "image"
        subtype = subtype or "png"
        msg.add_attachment(
            screenshot_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=screenshot_name,
        )

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(user, password)
                server.send_message(msg)
        return {"sent": True, "logged": False}
    except Exception as e:
        logger.error(f"Tira bug report: SMTP send failed: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to send bug report: {e}")


@router.post("/tira/bug-report")
async def tira_bug_report(
    title: str = Form(...),
    description: str = Form(""),
    page_url: str = Form(""),
    user_agent: str = Form(""),
    screenshot: Optional[UploadFile] = File(None),
):
    """Accept a bug report from the Tira panel and forward it by email."""
    if not (title or "").strip():
        raise HTTPException(status_code=400, detail="A short title is required.")

    screenshot_bytes: Optional[bytes] = None
    screenshot_name: Optional[str] = None
    screenshot_mime: Optional[str] = None
    if screenshot is not None:
        screenshot_bytes = await screenshot.read()
        screenshot_name = screenshot.filename or "screenshot.png"
        screenshot_mime = screenshot.content_type or "image/png"
        if len(screenshot_bytes) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Screenshot must be 8MB or smaller.")

    result = _send_bug_email(
        title=title.strip(),
        description=description.strip(),
        page_url=page_url.strip(),
        user_agent=user_agent.strip(),
        screenshot_name=screenshot_name,
        screenshot_bytes=screenshot_bytes,
        screenshot_mime=screenshot_mime,
    )
    return {"status": "success", **result}
