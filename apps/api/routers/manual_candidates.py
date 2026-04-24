from fastapi import APIRouter, HTTPException, UploadFile, File
from typing import List, Dict, Any, Optional
import logging

from models import ManualCandidateRequest
from routers._helpers import get_db_connection

router = APIRouter()
logger = logging.getLogger(__name__)


async def _score_and_save_resume(job_row: dict, name: str, email: str, phone: str, resume_text: str, source: str) -> dict:
    """
    Score a single pasted/uploaded resume against a job's rubric and persist it
    as a sourced_candidate. Returns the enriched candidate dict.
    """
    import json as _json
    import hashlib
    import time as _time
    from services.unified_candidate_search import unified_search_service, SearchCriteria
    from services.sourced_candidates_storage import process_jobdiva_candidate

    jobdiva_ref = job_row.get("jobdiva_id") or str(job_row.get("job_id"))
    sourcing_filters = job_row.get("sourcing_filters") or {}
    resume_match_filters = job_row.get("resume_match_filters") or []
    if isinstance(sourcing_filters, str):
        try: sourcing_filters = _json.loads(sourcing_filters)
        except: sourcing_filters = {}
    if isinstance(resume_match_filters, str):
        try: resume_match_filters = _json.loads(resume_match_filters)
        except: resume_match_filters = []

    candidate_id = f"manual_{int(_time.time()*1000)}_{hashlib.md5((resume_text or '').encode()).hexdigest()[:8]}"
    candidate: Dict[str, Any] = {
        "candidate_id": candidate_id,
        "provider_id": candidate_id,
        "jobdiva_id": jobdiva_ref,
        "name": name.strip(),
        "email": (email or "").strip(),
        "phone": (phone or "").strip(),
        "resume_text": resume_text,
        "source": source,
        "title": "",
        "location": "",
    }

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO sourced_candidates
            (jobdiva_id, candidate_id, source, name, email, phone, resume_text, status, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'sourced', NOW(), NOW())
        ON CONFLICT (jobdiva_id, candidate_id, source) DO UPDATE SET
            resume_text = EXCLUDED.resume_text,
            updated_at = NOW()
    """, (
        jobdiva_ref, candidate_id, source,
        candidate["name"], candidate["email"], candidate["phone"],
        candidate["resume_text"],
    ))
    conn.commit()
    cursor.close()
    conn.close()

    try:
        extraction = await process_jobdiva_candidate(candidate)
        enhanced_info = extraction.get("raw", {}) if isinstance(extraction, dict) else {}
    except Exception as proc_err:
        logger.warning(f"Resume enrichment failed: {proc_err}")
        enhanced_info = {}

    candidate["enhanced_info"] = enhanced_info
    if enhanced_info:
        candidate["name"] = enhanced_info.get("candidate_name") or candidate["name"]
        candidate["email"] = enhanced_info.get("email") or candidate["email"]
        candidate["phone"] = enhanced_info.get("phone") or candidate["phone"]
        candidate["title"] = enhanced_info.get("job_title") or candidate["title"]
        candidate["location"] = enhanced_info.get("current_location") or candidate["location"]
        candidate["skills"] = enhanced_info.get("structured_skills") or enhanced_info.get("skills") or []
        candidate["years_of_experience"] = enhanced_info.get("years_of_experience")

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
        titles=[c.get("value", "") for c in title_criteria if c.get("value")],
        skills=[c.get("value", "") for c in skill_criteria if c.get("value")],
        title_criteria=title_criteria,
        skill_criteria=skill_criteria,
        keywords=keywords,
        resume_match_filters=resume_match_filters if isinstance(resume_match_filters, list) else [],
        location=location_val,
        sources=[source],
    )
    score_result = unified_search_service._score_candidate(candidate, criteria)
    candidate["match_score"] = score_result["score"]
    candidate["missing_skills"] = score_result["missing_skills"]
    candidate["matched_skills"] = score_result.get("matched_skills", [])
    candidate["explainability"] = score_result["explainability"]
    candidate["match_score_details"] = score_result.get("score_details", {})

    conn = get_db_connection()
    cursor = conn.cursor()
    data_payload = {
        "candidate_name": candidate["name"],
        "email": candidate["email"],
        "phone": candidate["phone"],
        "job_title": candidate.get("title"),
        "years_of_experience": candidate.get("years_of_experience"),
        "current_location": candidate.get("location"),
        "skills": candidate.get("skills") or [],
        "company_experience": enhanced_info.get("company_experience", []),
        "candidate_education": enhanced_info.get("candidate_education", []),
        "candidate_certification": enhanced_info.get("candidate_certification", []),
        "resume_extraction_status": enhanced_info.get("resume_extraction_status", "completed" if enhanced_info else "pending"),
        "match_score": candidate["match_score"],
        "missing_skills": candidate["missing_skills"],
        "matched_skills": candidate["matched_skills"],
        "explainability": candidate["explainability"],
        "match_score_details": candidate["match_score_details"],
        "source": source,
        "manual_entry": True,
    }
    cursor.execute("""
        UPDATE sourced_candidates SET
            name = COALESCE(%s, name),
            email = COALESCE(%s, email),
            phone = COALESCE(%s, phone),
            headline = COALESCE(%s, headline),
            location = COALESCE(%s, location),
            data = %s::jsonb,
            updated_at = NOW()
        WHERE jobdiva_id = %s AND candidate_id = %s AND source = %s
    """, (
        candidate["name"], candidate["email"], candidate["phone"],
        candidate.get("title"), candidate.get("location"),
        _json.dumps(data_payload),
        jobdiva_ref, candidate_id, source,
    ))
    conn.commit()
    cursor.close()
    conn.close()

    return {
        "candidate_id": candidate_id,
        "jobdiva_id": jobdiva_ref,
        "name": candidate["name"],
        "email": candidate["email"],
        "phone": candidate["phone"],
        "title": candidate.get("title"),
        "location": candidate.get("location"),
        "source": source,
        "match_score": candidate["match_score"],
        "matched_skills": candidate["matched_skills"],
        "missing_skills": candidate["missing_skills"],
        "explainability": candidate["explainability"],
        "match_score_details": candidate["match_score_details"],
        "skills": candidate.get("skills") or [],
        "years_of_experience": candidate.get("years_of_experience"),
        "manual_entry": True,
    }


def _extract_resume_text_from_upload(filename: str, content: bytes) -> str:
    """Extract plain text from an uploaded resume file (PDF / DOCX / TXT)."""
    import io
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts).strip()
    if name.endswith(".docx"):
        import docx as _docx
        doc = _docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs).strip()
    if name.endswith(".txt") or name.endswith(".md"):
        try:
            return content.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""
    try:
        return content.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _guess_name_from_resume(text: str, fallback_filename: str) -> str:
    """Cheap heuristic — first non-empty line of the resume, else the filename."""
    if text:
        for line in text.splitlines():
            s = line.strip()
            if 2 <= len(s) <= 80 and not any(ch in s for ch in ["@", "http", "www."]):
                return s
    base = (fallback_filename or "").rsplit("/", 1)[-1]
    for ext in (".pdf", ".docx", ".txt", ".md"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
    return base or "Unknown"


@router.post("/jobs/{job_id}/manual-candidate")
async def add_manual_candidate(job_id: str, req: ManualCandidateRequest):
    """
    Accept a pasted resume for a job (intended for External/non-JobDiva jobs,
    but works for any job). Runs the same LLM enrichment and rubric scoring
    used on JobDiva applicants, and saves the record into sourced_candidates
    with source='JobDiva' so it shows up under the standard JobDiva pill.
    """
    try:
        import psycopg2.extras

        if not (req.resume_text or "").strip():
            raise HTTPException(status_code=400, detail="resume_text is required")
        if not (req.name or "").strip():
            raise HTTPException(status_code=400, detail="name is required")

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT job_id, jobdiva_id, sourcing_filters, resume_match_filters, title FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
            (job_id, job_id)
        )
        job_row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not job_row:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        result = await _score_and_save_resume(
            dict(job_row),
            name=req.name,
            email=req.email or "",
            phone=req.phone or "",
            resume_text=req.resume_text,
            source="JobDiva",
        )
        logger.info(f"Manual candidate {result['candidate_id']} saved for job {result['jobdiva_id']} (score={result['match_score']})")
        return {"status": "success", "candidate": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual candidate save failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to save manual candidate: {str(e)}")


@router.post("/jobs/{job_id}/bulk-resumes")
async def bulk_upload_resumes(job_id: str, files: List[UploadFile] = File(...)):
    """
    Accept a multipart upload of multiple resume files (PDF/DOCX/TXT), extract
    text from each, and run the same LLM enrichment + rubric scoring used on
    JobDiva applicants. Saves each as source='upload-resume' so it appears in
    a dedicated filter pill on the frontend.
    """
    try:
        import psycopg2.extras
        if not files:
            raise HTTPException(status_code=400, detail="No files uploaded")

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT job_id, jobdiva_id, sourcing_filters, resume_match_filters, title FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
            (job_id, job_id)
        )
        job_row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not job_row:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        job_row_dict = dict(job_row)

        processed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for f in files:
            try:
                content = await f.read()
                text = _extract_resume_text_from_upload(f.filename or "", content)
                if not text or len(text.strip()) < 40:
                    failed.append({"filename": f.filename, "error": "Could not extract resume text"})
                    continue
                name_guess = _guess_name_from_resume(text, f.filename or "")
                result = await _score_and_save_resume(
                    job_row_dict,
                    name=name_guess,
                    email="",
                    phone="",
                    resume_text=text,
                    source="upload-resume",
                )
                processed.append(result)
            except Exception as per_err:
                logger.error(f"Bulk upload: failed on {f.filename}: {per_err}")
                failed.append({"filename": f.filename, "error": str(per_err)})

        return {
            "status": "success",
            "processed_count": len(processed),
            "failed_count": len(failed),
            "candidates": processed,
            "failed": failed,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk resume upload failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Bulk resume upload failed: {str(e)}")
