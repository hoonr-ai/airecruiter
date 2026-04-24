
import json
import re
import time
import hashlib
import asyncio
import logging
from typing import List, Optional, Dict, Any
import sqlalchemy
from sqlalchemy import text
from core.config import (
    DATABASE_URL, SUPABASE_DB_URL,
    AZURE_OPENAI_API_KEY,
    OPENAI_API_KEY, OPENAI_MODEL,
    LLM_CONCURRENCY,
)
import httpx
from models import SourcedCandidate
from services.candidate_profiles_db import candidate_profiles_db

logger = logging.getLogger(__name__)


# v22: Module-level engine singleton. Pre-v22 every method/function created
# its own `create_engine(db_url)` with default pool settings, leaking
# connections until Postgres hit max_connections. Pool via a single engine
# with `pool_pre_ping=True` (drops dead conns silently) and
# `connect_timeout=5` (fails fast on a hung DB, doesn't block the worker
# for the TCP default ~2 min).
_ENGINE: Optional[sqlalchemy.engine.Engine] = None


def _get_engine() -> sqlalchemy.engine.Engine:
    global _ENGINE
    if _ENGINE is None:
        url = DATABASE_URL or SUPABASE_DB_URL
        if not url:
            raise RuntimeError("DATABASE_URL not configured for sourced_candidates_storage")
        _ENGINE = sqlalchemy.create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    return _ENGINE


def _ensure_sourced_candidates_schema() -> None:
    """Sync DDL bootstrap for sourced_candidates + candidate_enhanced_info.

    v22: pre-v22 this ran inside every save-path handler (see `_ensure_table`
    and `save_candidate_enhanced_info`). CREATE TABLE IF NOT EXISTS is cheap
    but ALTER TABLE grabs an ACCESS EXCLUSIVE lock — running it on every
    request stalls concurrent readers and, under lock contention, raises
    `psycopg2.errors.LockNotAvailable` → 500s. Lifespan calls this once at
    startup; per-request paths skip DDL entirely.
    """
    url = DATABASE_URL or SUPABASE_DB_URL
    if not url:
        logger.warning("sourced_candidates_schema_init_skipped: no DATABASE_URL")
        return
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            # sourced_candidates (canonical schema).
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS sourced_candidates (
                    id SERIAL PRIMARY KEY,
                    jobdiva_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    name TEXT,
                    email TEXT,
                    phone TEXT,
                    headline TEXT,
                    location TEXT,
                    resume_id TEXT,
                    resume_text TEXT,
                    profile_url TEXT,
                    image_url TEXT,
                    data JSONB,
                    status TEXT DEFAULT 'sourced',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(jobdiva_id, candidate_id, source)
                )
            """))

            # Legacy migrations (no-op if already applied). Wrapped so a
            # failure on one doesn't poison the others.
            for stmt in (
                "ALTER TABLE sourced_candidates RENAME COLUMN job_id TO jobdiva_id",
                "ALTER TABLE sourced_candidates RENAME COLUMN jobdiva_resume_id TO resume_id",
                "ALTER TABLE sourced_candidates DROP COLUMN IF EXISTS jobdiva_candidate_id",
                "ALTER TABLE sourced_candidates DROP COLUMN IF EXISTS candidate_type",
            ):
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass

            # Idempotent column adds.
            for stmt in (
                "ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS email TEXT",
                "ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS phone TEXT",
                "ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS resume_id TEXT",
                "ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS resume_text TEXT",
                "ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                "ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS resume_match_percentage NUMERIC",
            ):
                try:
                    conn.execute(text(stmt))
                except Exception as e:
                    logger.warning(f"sourced_candidates ALTER skipped: {stmt!r}: {e}")

            # candidate_enhanced_info (second DDL site, pre-v22 lived inside
            # save_candidate_enhanced_info and ran per save).
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS candidate_enhanced_info (
                    id SERIAL PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE,
                    candidate_name TEXT,
                    email TEXT,
                    phone TEXT,
                    job_title TEXT,
                    years_of_experience INT,
                    current_location TEXT,
                    key_skills JSONB DEFAULT '[]'::jsonb,
                    company_experience JSONB DEFAULT '[]'::jsonb,
                    candidate_education JSONB DEFAULT '[]'::jsonb,
                    candidate_certification JSONB DEFAULT '[]'::jsonb,
                    urls JSONB DEFAULT '{}'::jsonb,
                    resume_text TEXT,
                    resume_hash TEXT,
                    resume_extraction_status TEXT DEFAULT 'pending',
                    source TEXT DEFAULT 'JobDiva',
                    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + '30 days'::interval)
                )
            """))
            try:
                conn.execute(text("ALTER TABLE candidate_enhanced_info ADD COLUMN IF NOT EXISTS resume_hash TEXT"))
            except Exception as e:
                logger.warning(f"candidate_enhanced_info ALTER resume_hash skipped: {e}")
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_candidate_enhanced_info_resume_hash "
                    "ON candidate_enhanced_info (resume_hash)"
                ))
            except Exception as e:
                logger.warning(f"candidate_enhanced_info CREATE INDEX skipped: {e}")

            conn.commit()
        logger.info("sourced_candidates schema ready")
    except Exception as e:
        logger.error(f"sourced_candidates schema init failed: {e}")


async def init_sourced_candidates_schema() -> None:
    """Async wrapper called from main.py lifespan."""
    await asyncio.to_thread(_ensure_sourced_candidates_schema)


def _truncate_log_value(value: Any, limit: int = 80) -> str:
    text_value = str(value or "").strip()
    if len(text_value) <= limit:
        return text_value or "-"
    return text_value[: limit - 3] + "..."


def _clean_extracted_value(value: Any) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None

    invalid_markers = {
        "not provided",
        "n/a",
        "na",
        "unknown",
        "none",
        "null",
        "professional candidate",
        "available upon request",
    }
    if cleaned.lower() in invalid_markers:
        return None
        
    # Alphanumeric ID detection: Reject long strings (>15 chars) with no spaces that contain digits
    # These are almost always internal IDs or hashes from external scrapers.
    if len(cleaned) > 15 and " " not in cleaned:
        if any(c.isdigit() for c in cleaned):
            return None
        # Also reject if it has high entropy (mix of many upper/lower case letters with no spaces)
        if len(re.findall(r'[A-Z]', cleaned)) > 5 and len(re.findall(r'[a-z]', cleaned)) > 5:
            return None
            
    return cleaned


def _has_real_resume_text(resume_text: str) -> bool:
    cleaned = str(resume_text or "").strip()
    if not cleaned:
        return False

    placeholder_markers = [
        "resume content unavailable",
        "professional experience details available upon request",
        "experienced professional with a strong background",
        "contact information and detailed work history available upon request",
        "available upon request",
    ]
    lowered = cleaned.lower()
    return not any(marker in lowered for marker in placeholder_markers)


def _extract_resume_contact_details(resume_text: str) -> Dict[str, Any]:
    cleaned = str(resume_text or "")
    extracted: Dict[str, Any] = {"email": None, "phone": None, "urls": {}}
    if not cleaned:
        return extracted

    email_match = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", cleaned, re.IGNORECASE)
    if email_match:
        extracted["email"] = email_match.group(1).strip()

    phone_match = re.search(
        r"(\+?\d[\d\-\(\)\s]{7,}\d)",
        cleaned,
    )
    if phone_match:
        extracted["phone"] = re.sub(r"\s+", " ", phone_match.group(1)).strip()

    urls: Dict[str, str] = {}
    for raw_url in re.findall(r"(https?://[^\s\]\)<>]+|www\.[^\s\]\)<>]+|linkedin\.com/[^\s\]\)<>]+|github\.com/[^\s\]\)<>]+)", cleaned, re.IGNORECASE):
        normalized = raw_url.strip().rstrip(".,;")
        if normalized.lower().startswith("www."):
            normalized = f"https://{normalized}"
        elif not normalized.lower().startswith("http"):
            normalized = f"https://{normalized}"

        lowered = normalized.lower()
        if "linkedin.com/" in lowered:
            urls["linkedin"] = normalized
        elif "github.com/" in lowered:
            urls["github"] = normalized
        elif "mailto:" not in lowered:
            urls.setdefault("portfolio", normalized)

    extracted["urls"] = _normalize_candidate_urls(urls)
    return extracted


def _normalize_llm_skills(llm_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    formatted_skills: List[Dict[str, Any]] = []
    llm_skills = llm_result.get("skills", []) or llm_result.get("hard_skills", [])
    for skill in llm_skills:
        if isinstance(skill, dict) and skill.get("name"):
            formatted_skills.append({
                "skill": str(skill["name"]).strip(),
                "similar_skills": [],
            })
        elif isinstance(skill, str) and skill.strip():
            formatted_skills.append({
                "skill": skill.strip(),
                "similar_skills": [],
            })
    return formatted_skills


def _normalize_candidate_urls(urls: Any) -> Dict[str, str]:
    if not isinstance(urls, dict):
        return {}

    allowed_keys = {"linkedin", "github", "portfolio"}
    normalized_urls: Dict[str, str] = {}
    for key, value in urls.items():
        normalized_key = str(key or "").strip().lower()
        normalized_value = str(value or "").strip()
        if normalized_key in allowed_keys and normalized_value:
            normalized_urls[normalized_key] = normalized_value
    return normalized_urls


def _log_extraction_snapshot(candidate_id: str, enhanced_info: Dict[str, Any]) -> None:
    skills = enhanced_info.get("structured_skills", []) or []
    companies = enhanced_info.get("company_experience", []) or []
    education = enhanced_info.get("candidate_education", []) or []
    certifications = enhanced_info.get("candidate_certification", []) or []
    logger.info(
        "[ResumeExtract] candidate_id=%s status=%s name=%s title=%s years=%s location=%s skills=%s companies=%s education=%s certs=%s",
        candidate_id,
        enhanced_info.get("resume_extraction_status", "unknown"),
        _truncate_log_value(enhanced_info.get("candidate_name")),
        _truncate_log_value(enhanced_info.get("job_title")),
        _truncate_log_value(enhanced_info.get("years_of_experience")),
        _truncate_log_value(enhanced_info.get("current_location")),
        len(skills),
        len(companies),
        len(education),
        len(certifications),
    )

class SourcedCandidatesStorage:
    def __init__(self):
        self.db_url = DATABASE_URL or SUPABASE_DB_URL

    def _ensure_table(self, conn):
        """No-op. v22: schema DDL moved to lifespan startup
        (`init_sourced_candidates_schema`). Kept as a no-op so legacy callers
        don't need to be rewritten, but the per-request ALTER TABLE lock
        contention is gone."""
        return

    def save_candidates(self, jobdiva_id: str, candidates: List[SourcedCandidate]) -> int:
        """Save search results to the database with enhanced JobDiva integration."""
        if not self.db_url:
            return 0
        
        saved_count = 0
        try:
            engine = _get_engine()
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                for c in candidates:
                    try:
                        # Enhanced insert with clean schema
                        conn.execute(text("""
                            INSERT INTO sourced_candidates 
                            (jobdiva_id, candidate_id, source, name, email, phone, headline, location, profile_url, image_url, 
                             data, status, resume_id, resume_text, updated_at)
                            VALUES (:jobdiva_id, :candidate_id, :source, :name, :email, :phone, :headline, :location, 
                                    :profile_url, :image_url, :data, :status, :resume_id, :resume_text, CURRENT_TIMESTAMP)
                            ON CONFLICT (jobdiva_id, candidate_id, source) 
                            DO UPDATE SET 
                                name = EXCLUDED.name,
                                email = EXCLUDED.email,
                                phone = EXCLUDED.phone,
                                headline = EXCLUDED.headline,
                                location = EXCLUDED.location,
                                profile_url = EXCLUDED.profile_url,
                                image_url = EXCLUDED.image_url,
                                data = EXCLUDED.data,
                                resume_id = EXCLUDED.resume_id,
                                resume_text = EXCLUDED.resume_text,
                                updated_at = CURRENT_TIMESTAMP
                        """), {
                            "jobdiva_id": jobdiva_id,
                            "candidate_id": c.candidate_id,
                            "source": c.source,
                            "name": c.name,
                            "email": getattr(c, 'email', None),
                            "phone": getattr(c, 'phone', None),
                            "headline": c.headline,
                            "location": c.location,
                            "profile_url": c.profile_url,
                            "image_url": c.image_url,
                            "data": json.dumps(c.data) if c.data else None,
                            "status": c.status,
                            "resume_id": getattr(c, 'resume_id', None),
                            "resume_text": getattr(c, 'resume_text', None)
                        })
                        saved_count += 1
                    except Exception as e:
                        print(f"Error saving candidate {c.candidate_id}: {e}")
                        
                conn.commit()
        except Exception as e:
            print(f"Error saving candidates: {e}")
        
        return saved_count

    def save_enhanced_candidate(self, job_id: str, candidate_data: Dict[str, Any]) -> bool:
        """Save a single enhanced candidate with full JobDiva data integration using new schema."""
        if not self.db_url:
            return False
            
        try:
            engine = _get_engine()
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                conn.execute(text("""
                    INSERT INTO sourced_candidates 
                    (jobdiva_id, candidate_id, source, name, email, phone, headline, location, profile_url, 
                     image_url, resume_id, resume_text, data, status, updated_at)
                    VALUES (:jobdiva_id, :candidate_id, :source, :name, :email, :phone, :headline, :location, 
                            :profile_url, :image_url, :resume_id, :resume_text, :data, :status, CURRENT_TIMESTAMP)
                    ON CONFLICT (jobdiva_id, candidate_id, source) 
                    DO UPDATE SET 
                        name = EXCLUDED.name,
                        email = EXCLUDED.email,
                        phone = EXCLUDED.phone,
                        headline = EXCLUDED.headline,
                        location = EXCLUDED.location,
                        profile_url = EXCLUDED.profile_url,
                        image_url = EXCLUDED.image_url,
                        resume_id = EXCLUDED.resume_id,
                        resume_text = EXCLUDED.resume_text,
                        data = EXCLUDED.data,
                        status = 'sourced',
                        updated_at = CURRENT_TIMESTAMP
                """), candidate_data)
                
                conn.commit()
                return True
        except Exception as e:
            print(f"Error saving enhanced candidate: {e}")
            return False

    def deduplicate_candidates(self, job_id: str) -> int:
        """Remove duplicates, prioritizing job applicants over talent search using new schema."""
        if not self.db_url:
            return 0
            
        try:
            engine = _get_engine()
            with engine.connect() as conn:
                result = conn.execute(text("""
                    DELETE FROM sourced_candidates s1
                    WHERE s1.jobdiva_id = :job_id
                    AND EXISTS (
                        SELECT 1 FROM sourced_candidates s2
                        WHERE s2.jobdiva_id = s1.jobdiva_id
                        AND s2.candidate_id = s1.candidate_id
                        AND s2.source = 'JobDiva-Applicants'
                        AND s1.source = 'JobDiva-TalentSearch'
                        AND s1.id != s2.id
                    )
                """), {"job_id": job_id})
                
                conn.commit()
                return result.rowcount
        except Exception as e:
            print(f"Error deduplicating candidates: {e}")
            return 0

    def get_candidates_for_job(self, jobdiva_id: str) -> List[Dict[str, Any]]:
        """Retrieve all sourced candidates for a specific job."""
        if not self.db_url:
            return []
            
        try:
            import psycopg2
            import psycopg2.extras
            import json
            
            conn = psycopg2.connect(self.db_url, connect_timeout=5)
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # Robust query that handles the mapping logic in SQL
            # Same logic as get_all_candidates but filtered by job_id
            query = """
                SELECT DISTINCT ON (sc.candidate_id) sc.*, 
                       sc.resume_match_percentage AS match_score
                FROM sourced_candidates sc
                LEFT JOIN monitored_jobs mj ON (sc.jobdiva_id = mj.job_id OR sc.jobdiva_id = mj.jobdiva_id)
                WHERE mj.job_id = %s OR mj.jobdiva_id = %s OR sc.jobdiva_id = %s
                ORDER BY sc.candidate_id, sc.created_at DESC
            """
            
            cur.execute(query, (jobdiva_id, jobdiva_id, jobdiva_id))
            
            candidates = []
            for row in cur.fetchall():
                c_dict = dict(row)
                if c_dict.get('data'):
                    try:
                        data = c_dict['data']
                        if isinstance(data, str):
                            data = json.loads(data)
                            c_dict['data'] = data
                            
                        # Uplift key metrics for UI consumption
                        if isinstance(data, dict):
                            if 'match_score' in data and c_dict.get('match_score') is None:
                                c_dict['match_score'] = data['match_score']
                            if 'engage_score' in data and c_dict.get('engage_score') is None:
                                c_dict['engage_score'] = data['engage_score']
                            if 'engage_status' in data and c_dict.get('engage_status') is None:
                                c_dict['engage_status'] = data['engage_status']
                    except Exception as e:
                        print(f"Error parsing candidate data: {e}")
                if c_dict.get('created_at'):
                    c_dict['created_at'] = str(c_dict['created_at'])
                candidates.append(c_dict)
                
            cur.close()
            conn.close()
            return candidates
        except Exception as e:
            print(f"Error retrieving candidates for job {jobdiva_id}: {e}")
            return []

    # Whitelist of sortable columns -> SQL expression. Inlined into the ORDER
    # BY clause. NULLS LAST so empty/missing values don't crowd the top when
    # sorting asc.
    _SORT_EXPR = {
        "name":       "COALESCE(NULLIF(d.name, ''), '') ",
        "match":      "d.match_score",
        "job":        "COALESCE(NULLIF(d.job_title, ''), '') ",
        "source":     "COALESCE(NULLIF(d.source, ''), '') ",
        "location":   "COALESCE(NULLIF(d.location, ''), '') ",
        "created_at": "d.created_at",
    }
    # Match-band query-string values → (min, max) inclusive numeric bounds,
    # or the "unscored" sentinel (NULL match_score). Names mirror the FE
    # MATCH_BANDS constant at apps/web/app/candidates/page.tsx so no
    # translation layer is needed between client & server.
    _MATCH_BANDS = {
        "strong":   (80, 100),
        "good":     (60, 79.9999),
        "low":      (0, 59.9999),
        "unscored": None,  # sentinel → match_score IS NULL
    }

    def get_all_candidates(
        self,
        limit: int = 50,
        offset: int = 0,
        search: Optional[str] = None,
        job_id: Optional[str] = None,
        source: Optional[str] = None,
        location: Optional[str] = None,
        match_band: Optional[str] = None,
        sort_key: Optional[str] = None,
        sort_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retrieve paginated + filtered + sorted candidates across all jobs.

        Returns {"candidates": [...current page...], "total": <filtered count>}.

        Pagination is server-side; filters apply across the whole DB (not just
        the current page). `match_band` buckets resume_match_percentage into
        "80-100"/"60-79"/"0-59". `sort_key` is whitelisted — unknown keys fall
        back to the default (source priority + match desc). Expensive SELECTs
        are wrapped in a CTE so DISTINCT ON + filters compose cleanly.
        """
        if not self.db_url:
            return {"candidates": [], "total": 0}

        try:
            import psycopg2
            import psycopg2.extras

            conn = psycopg2.connect(self.db_url, connect_timeout=5)
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # --- ORDER BY (whitelist) -----------------------------------------
            expr = self._SORT_EXPR.get((sort_key or "").lower())
            direction = "DESC" if (sort_dir or "desc").lower() == "desc" else "ASC"
            if expr:
                order_by = f"{expr} {direction} NULLS LAST, d.candidate_id ASC"
            else:
                # Default: source priority (applicants > linkedin > talentsearch
                # > other) then match_score desc. Mirrors the FE default sort so
                # server-driven paging agrees with what the user used to see.
                order_by = """
                    CASE
                        WHEN LOWER(COALESCE(d.source,'')) LIKE '%%applicants%%' THEN 1
                        WHEN LOWER(COALESCE(d.source,'')) LIKE '%%linkedin%%'   THEN 2
                        WHEN LOWER(COALESCE(d.source,'')) LIKE '%%talentsearch%%'
                             OR LOWER(COALESCE(d.source,'')) LIKE '%%talent_search%%' THEN 3
                        ELSE 4
                    END ASC,
                    d.match_score DESC NULLS LAST,
                    d.candidate_id ASC
                """

            # --- Match band -> numeric bounds or NULL filter ------------------
            band_unscored = False
            min_score: Optional[float] = None
            max_score: Optional[float] = None
            if match_band:
                band = self._MATCH_BANDS.get(match_band)
                if band is None and match_band == "unscored":
                    band_unscored = True
                elif band is not None:
                    min_score, max_score = band

            # --- Named params -------------------------------------------------
            search_like = f"%{search.strip()}%" if search and search.strip() else None
            params = {
                "job_id":        job_id or None,
                "source":        source or None,
                "location":      location or None,
                "search":        search_like,
                "search_like":   search_like,
                "min_score":     min_score,
                "max_score":     max_score,
                "band_unscored": band_unscored,
                "limit":         max(1, min(int(limit), 200)),
                "offset":        max(0, int(offset)),
            }

            # CTE dedupes by candidate_id (keep most-recent row), then outer
            # SELECT filters, counts, orders, paginates.
            query = f"""
                WITH deduped AS (
                    SELECT DISTINCT ON (sc.candidate_id)
                        sc.*,
                        COALESCE(
                            sc.resume_match_percentage,
                            NULLIF(sc.data->>'match_score','')::numeric
                        ) AS match_score,
                        mj.title AS job_title
                    FROM sourced_candidates sc
                    LEFT JOIN monitored_jobs mj
                      ON (sc.jobdiva_id = mj.job_id OR sc.jobdiva_id = mj.jobdiva_id)
                    ORDER BY sc.candidate_id, sc.created_at DESC
                )
                SELECT d.*, COUNT(*) OVER() AS total_count
                FROM deduped d
                WHERE (%(job_id)s IS NULL   OR d.jobdiva_id = %(job_id)s)
                  AND (%(source)s IS NULL   OR d.source = %(source)s)
                  AND (%(location)s IS NULL OR d.location = %(location)s)
                  AND (
                      %(band_unscored)s = FALSE
                      OR d.match_score IS NULL
                  )
                  AND (%(min_score)s IS NULL OR d.match_score >= %(min_score)s)
                  AND (%(max_score)s IS NULL OR d.match_score <= %(max_score)s)
                  AND (
                      %(search)s IS NULL
                      OR COALESCE(d.name,'')       ILIKE %(search_like)s
                      OR COALESCE(d.headline,'')   ILIKE %(search_like)s
                      OR COALESCE(d.job_title,'')  ILIKE %(search_like)s
                      OR COALESCE(d.location,'')   ILIKE %(search_like)s
                      OR COALESCE(d.jobdiva_id,'') ILIKE %(search_like)s
                  )
                ORDER BY {order_by}
                LIMIT %(limit)s OFFSET %(offset)s
            """
            cur.execute(query, params)
            rows = cur.fetchall()

            total = int(rows[0]["total_count"]) if rows else 0

            candidates: List[Dict[str, Any]] = []
            for row in rows:
                c_dict = dict(row)
                c_dict.pop("total_count", None)
                # JSON uplift: column-level `match_score` already COALESCEd at
                # SQL layer. engage_score / engage_status still live only in
                # the `data` jsonb, so uplift them here for the FE.
                if c_dict.get("data"):
                    try:
                        data = c_dict["data"]
                        if isinstance(data, str):
                            data = json.loads(data)
                            c_dict["data"] = data
                        if isinstance(data, dict):
                            if data.get("engage_score") is not None and c_dict.get("engage_score") is None:
                                c_dict["engage_score"] = data["engage_score"]
                            if data.get("engage_status") and c_dict.get("engage_status") is None:
                                c_dict["engage_status"] = data["engage_status"]
                    except Exception as e:
                        logger.debug(f"Error parsing candidate.data json: {e}")
                if c_dict.get("created_at"):
                    c_dict["created_at"] = str(c_dict["created_at"])
                # Numeric -> float for JSON serialization
                if c_dict.get("match_score") is not None:
                    try:
                        c_dict["match_score"] = float(c_dict["match_score"])
                    except Exception:
                        pass
                candidates.append(c_dict)

            cur.close()
            conn.close()
            return {"candidates": candidates, "total": total}
        except Exception as e:
            logger.error(f"Error retrieving all candidates: {e}")
            return {"candidates": [], "total": 0}

    def get_filter_options(self) -> Dict[str, Any]:
        """Distinct values used to populate FE filter dropdowns.

        Pulled from the full `sourced_candidates` table (DB-wide, not the
        current page) so filtering still operates on all rows post-pagination.
        """
        if not self.db_url:
            return {"jobs": [], "sources": [], "locations": []}

        try:
            import psycopg2
            import psycopg2.extras

            conn = psycopg2.connect(self.db_url, connect_timeout=5)
            conn.autocommit = True
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # Jobs: jobdiva_id + best-effort title via monitored_jobs.
            cur.execute("""
                SELECT sc.jobdiva_id AS id, MIN(mj.title) AS title
                FROM sourced_candidates sc
                LEFT JOIN monitored_jobs mj
                  ON (sc.jobdiva_id = mj.job_id OR sc.jobdiva_id = mj.jobdiva_id)
                WHERE sc.jobdiva_id IS NOT NULL AND sc.jobdiva_id <> ''
                GROUP BY sc.jobdiva_id
                ORDER BY title NULLS LAST, sc.jobdiva_id
            """)
            jobs = [
                {"id": r["id"], "label": f"{r['title']} — #{r['id']}" if r["title"] else f"#{r['id']}"}
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT DISTINCT source FROM sourced_candidates
                WHERE source IS NOT NULL AND source <> ''
                ORDER BY source
            """)
            sources = [r["source"] for r in cur.fetchall()]

            cur.execute("""
                SELECT DISTINCT location FROM sourced_candidates
                WHERE location IS NOT NULL AND location <> ''
                ORDER BY location
            """)
            locations = [r["location"] for r in cur.fetchall()]

            cur.close()
            conn.close()
            return {"jobs": jobs, "sources": sources, "locations": locations}
        except Exception as e:
            logger.error(f"Error retrieving filter options: {e}")
            return {"jobs": [], "sources": [], "locations": []}

sourced_candidates_storage = SourcedCandidatesStorage()


# Concurrency controls to prevent 429 Too Many Requests for LLM enrichment.
# Width configurable via LLM_CONCURRENCY env (default 5). Previous hard-coded 2
# serialized crisp+extract behind a 2-wide gate, adding wall-time cost even
# when the upstream API could handle more.
_llm_semaphore = asyncio.Semaphore(max(1, LLM_CONCURRENCY))


def _resume_text_hash(resume_text: str) -> Optional[str]:
    """SHA256 over normalized resume text for extraction-cache lookups.

    Returns None when the input is too short to meaningfully cache against
    (mirrors the ``min_text_length=50`` gate in ``_process_candidate_common``).
    """
    if not resume_text:
        return None
    normalized = re.sub(r"\s+", " ", resume_text).strip()
    if len(normalized) < 50:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _lookup_cached_enhanced_info_by_resume_hash(resume_hash: str) -> Optional[Dict[str, Any]]:
    """Return a reusable enhanced_info payload for a resume we've already
    LLM-parsed. Keyed on resume_text SHA256 so the same resume content parsed
    against a different candidate_id (JobDiva re-ingestion, duplicate
    applicants) doesn't trigger a second LLM pass.

    Returns None on miss or any DB error (callers treat None as "go parse").
    """
    if not resume_hash:
        return None
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT candidate_name, email, phone, job_title, current_location,
                       years_of_experience, key_skills, company_experience,
                       candidate_education, candidate_certification, urls, source,
                       resume_extraction_status
                FROM candidate_enhanced_info
                WHERE resume_hash = :h
                  AND resume_extraction_status = 'completed'
                ORDER BY extracted_at DESC
                LIMIT 1
            """), {"h": resume_hash}).fetchone()
            if not row:
                return None
            row_map = row._mapping if hasattr(row, "_mapping") else dict(row)

            def _j(value):
                if value is None:
                    return []
                if isinstance(value, (list, dict)):
                    return value
                try:
                    return json.loads(value)
                except Exception:
                    return []

            return {
                "candidate_name": row_map.get("candidate_name"),
                "email": row_map.get("email"),
                "phone": row_map.get("phone"),
                "job_title": row_map.get("job_title"),
                "current_location": row_map.get("current_location"),
                "years_of_experience": row_map.get("years_of_experience"),
                "structured_skills": _j(row_map.get("key_skills")),
                "key_skills": _j(row_map.get("key_skills")),
                "company_experience": _j(row_map.get("company_experience")),
                "candidate_education": _j(row_map.get("candidate_education")),
                "candidate_certification": _j(row_map.get("candidate_certification")),
                "urls": _j(row_map.get("urls")) if row_map.get("urls") else {},
                "source": row_map.get("source"),
                "resume_extraction_status": row_map.get("resume_extraction_status"),
                "_cache_hit": "resume_hash",
            }
    except Exception as exc:
        logger.debug(f"resume_hash cache lookup failed (soft miss): {exc}")
        return None

async def crisp_resume_with_ai(resume_text: str, max_length: int = 7500) -> str:
    """
    Condense resume using AI to fit within token limits while preserving ALL important details.
    
    PRESERVED (no truncation):
    - All company names and job titles
    - All employment dates (start/end)
    - All education (degree, institution, year)
    - All certifications (name, issuer, year)
    - All skills
    - Contact information (email, phone, LinkedIn)
    
    REMOVED (fluff reduction):
    - Long job descriptions
    - Generic soft skills ("team player", "hardworking")
    - Redundant phrases
    - Formatting noise
    - Buzzwords
    
    Returns crisped resume under max_length characters.
    Original resume is NOT modified - used only for extraction.
    """
    # If resume is already short enough, return as-is
    if len(resume_text) <= max_length:
        logger.info(f"📄 [Resume Crisping] Resume already short ({len(resume_text)} chars), skipping crisping")
        return resume_text
    
    logger.info(f"📄 [Resume Crisping] Starting crisping for resume ({len(resume_text)} chars)")
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = (
        "Condense the following resume to under 7500 characters. "
        "You MUST preserve ALL of the following without exception:\n\n"
        "REQUIRED (do not remove or summarize):\n"
        "- Every single company name\n"
        "- Every single job title\n"
        "- Every employment date (start and end dates)\n"
        "- Every degree and institution\n"
        "- Every certification name, issuer, and year\n"
        "- All technical skills\n"
        "- Contact information (email, phone, LinkedIn URL)\n\n"
        "REMOVE (to save space):\n"
        "- Long job descriptions and responsibilities\n"
        "- Generic soft skills (team player, hardworking, etc.)\n"
        "- Redundant phrases\n"
        "- Formatting noise\n\n"
        "Format the output as a clean, scannable resume. "
        "Use bullet points for companies and education. "
        "Keep dates in 'MMM YYYY' format.\n\n"
        "Resume to condense:\n"
        "---BEGIN RESUME---\n"
        + resume_text +
        "\n---END RESUME---"
    )
    
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You are a resume editor. Condense resumes by removing fluff while preserving all companies, titles, dates, education, and certifications. Never truncate important details."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 2000
    }
    
    async with _llm_semaphore:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    
                    if response.status_code == 429:
                        wait_time = 5 * (2 ** attempt)
                        logger.warning(f"⚠️ [Resume Crisping] 429 error, retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    response.raise_for_status()
                    result = response.json()
                    crisped = result["choices"][0]["message"]["content"]
                    
                    logger.info(f"✅ [Resume Crisping] Crisped resume from {len(resume_text)} to {len(crisped)} chars")
                    return crisped
                    
            except Exception as e:
                if attempt == 2:
                    logger.error(f"❌ [Resume Crisping] Failed after 3 attempts: {e}")
                    # Fall back to truncation if crisping fails
                    logger.warning(f"⚠️ [Resume Crisping] Falling back to simple truncation")
                    return resume_text[:max_length]
                
                wait_time = 5 * (2 ** attempt)
                logger.warning(f"⚠️ [Resume Crisping] Error on attempt {attempt + 1}: {e}, retrying...")
                await asyncio.sleep(wait_time)
    
    # Should not reach here, but just in case
    return resume_text[:max_length]

async def extract_enhanced_info_with_llm(resume_text: str) -> Dict[str, Any]:
    """Call OpenAI LLM to extract enhanced info from resume text."""
    logger.info("[LLM] resume_chars=%s model=%s starting extraction", len(resume_text), OPENAI_MODEL)
    
    if not OPENAI_API_KEY or not OPENAI_MODEL:
        logger.error("❌ [LLM] OpenAI API key or model not configured")
        raise RuntimeError("OpenAI API key or model not configured.")
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    # Sanitize resume text to avoid JSON parsing issues while keeping enough of the
    # resume to capture lower-page sections like education and certifications.
    sanitized_resume = resume_text[:16000]
    # Remove null bytes and other control characters that could break JSON
    sanitized_resume = ''.join(char for char in sanitized_resume if ord(char) >= 32 or char in '\n\r\t')
    
    prompt = (
        "Extract the following information from the resume text and return ONLY valid JSON matching this exact structure (no summary field):\n"
        "{\n"
        '  "candidate_name": "Full Name",\n'
        '  "email": "email@example.com",\n'
        '  "phone": "+1-xxx-xxx-xxxx",\n'
        '  "job_title": "Most recent or current job title",\n'
        '  "years_of_experience": 5,\n'
        '  "current_location": "City, State",\n'
        '  "skills": [\n'
        '    {\n'
        '      "name": "Skill Name"\n'
        '    }\n'
        '  ],\n'
        '  "company_experience": [\n'
        '    {\n'
        '      "company": "Company Name",\n'
        '      "title": "Job Title",\n'
        '      "start_date": "Jan 2020",\n'
        '      "end_date": "Dec 2023 or Present"\n'
        '    }\n'
        '  ],\n'
        '  "candidate_education": [\n'
        '    {\n'
        '      "degree": "Degree Name",\n'
        '      "institution": "University Name",\n'
        '      "year": "2020"\n'
        '    }\n'
        '  ],\n'
        '  "candidate_certification": [\n'
        '    {\n'
        '      "name": "Certification Name",\n'
        '      "issuer": "Issuing Organization",\n'
        '      "year": "2021"\n'
        '    }\n'
        '  ],\n'
        '  "urls": {\n'
        '    "linkedin": "https://linkedin.com/in/...",\n'
        '    "github": "https://github.com/...",\n'
        '    "portfolio": "https://..."\n'
        '  }\n'
        "}\n\n"
        "Instructions:\n"
        "1. Fill every field in the JSON from the resume text whenever the resume contains that information.\n"
        "2. job_title must be the candidate's current or most recent title from the latest experience entry.\n"
        "3. years_of_experience must be a numeric total based on the resume timeline or explicit summary.\n"
        "4. Extract concrete professional skills into skills[].name. Include all meaningful technical and functional skills stated in the resume.\n"
        "5. Extract complete company_experience entries with company, title, start_date, end_date. List them in reverse chronological order.\n"
        "6. Extract all education entries present in the resume into candidate_education.\n"
        "7. Extract all certifications/licenses present in the resume into candidate_certification.\n"
        "8. Extract only LinkedIn, GitHub, and portfolio URLs into urls.\n"
        "9. Do not use placeholders like 'available upon request', 'not provided', 'n/a', or empty dummy values.\n"
        "10. Return ONLY the JSON object, no markdown, no explanations.\n"
        "11. Ensure all strings are properly escaped and JSON is valid.\n\n"
        "Resume Text (parse this to extract the information above):\n"
        "---BEGIN RESUME---\n"
        + sanitized_resume +
        "\n---END RESUME---"
    )
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You are an expert resume parser. Always return valid JSON only, no markdown formatting, no explanations."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "max_tokens": 1800
    }
    
    async with _llm_semaphore:
        for attempt in range(5):
            logger.info(f"🧠 [LLM] Attempt {attempt + 1}/5...")
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    logger.info(f"🧠 [LLM] Sending request to OpenAI API...")
                    response = await client.post(url, headers=headers, json=payload)
                    
                    if response.status_code == 429:
                        wait_time = 5 * (2 ** attempt)
                        logger.warning(f"⚠️ [LLM] 429 Too Many Requests, retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    response.raise_for_status()
                    result = response.json()
                    logger.info("[LLM] response received")
                    
                    # Parse the LLM's JSON output from the response
                    content = result["choices"][0]["message"]["content"]
                    logger.info("[LLM] response_chars=%s", len(content))
                    
                    try:
                        parsed = json.loads(content)
                        
                        logger.info(
                            "[LLM] parsed name=%s title=%s years=%s location=%s skills=%s companies=%s education=%s certs=%s urls=%s",
                            _truncate_log_value(parsed.get("candidate_name")),
                            _truncate_log_value(parsed.get("job_title")),
                            _truncate_log_value(parsed.get("years_of_experience")),
                            _truncate_log_value(parsed.get("current_location")),
                            len(parsed.get("skills", []) or []),
                            len(parsed.get("company_experience", []) or []),
                            len(parsed.get("candidate_education", []) or []),
                            len(parsed.get("candidate_certification", []) or []),
                            len([v for v in (parsed.get("urls", {}) or {}).values() if v]),
                        )
                        return parsed
                    except json.JSONDecodeError as json_err:
                        logger.warning(f"⚠️ [LLM] JSON parse error: {json_err}, returning raw content")
                        logger.debug(f"[LLM] Raw content: {content[:500]}...")
                        return {"raw": content}
            except Exception as e:
                if attempt == 4:
                    logger.error(f"❌ [LLM] Error on final attempt: {e}")
                    return {"error": str(e)}
                
                wait_time = 5 * (2 ** attempt)
                logger.warning(f"⚠️ [LLM] Error on attempt {attempt + 1}: {e}, retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)


async def _process_candidate_common(
    candidate: Dict[str, Any],
    resume_text_for_llm: str,
    resume_text_to_save: str,
    source: str,
    fallbacks: Dict[str, Any],
    min_text_length: int = 50,
) -> Dict[str, Any]:
    """Shared pipeline: crisp → LLM extract → build enhanced_info → save → return.

    Callers assemble source-specific resume text and fallbacks. `fallbacks` may
    contain any of: name, email, phone, location, title, urls, skills,
    company_experience, education, certifications.
    """
    candidate_id = candidate.get("candidate_id", candidate.get("id", "unknown"))
    candidate_name = candidate.get("name", "Unknown")

    logger.info(f"🔄 [{source} Candidate:{candidate_id}] Starting processing for {candidate_name}")

    if len(resume_text_for_llm.strip()) < min_text_length:
        logger.warning(f"⚠️ [{source} Candidate:{candidate_id}] Insufficient text, skipping LLM processing")
        return candidate

    logger.info(f"📄 [{source} Candidate:{candidate_id}] Text length: {len(resume_text_for_llm)} characters")

    # Resume-hash cache: the same resume parsed once costs money; look up by
    # SHA256 of the resume text before calling crisp+extract. On hit we skip
    # both LLM calls entirely.
    resume_hash = _resume_text_hash(resume_text_to_save or resume_text_for_llm)
    enhanced_info_result: Optional[Dict[str, Any]] = None
    if resume_hash:
        cached = _lookup_cached_enhanced_info_by_resume_hash(resume_hash)
        if cached:
            logger.info(
                f"💾 [{source} Candidate:{candidate_id}] resume_hash cache HIT, "
                f"skipping crisp+LLM"
            )
            enhanced_info_result = cached

    if enhanced_info_result is None:
        crisped = await crisp_resume_with_ai(resume_text_for_llm, max_length=12000)
        logger.info(f"📄 [{source} Candidate:{candidate_id}] Crisped to {len(crisped)} chars")
        enhanced_info_result = await extract_enhanced_info_with_llm(crisped)

    extraction_error: Optional[str] = None
    if enhanced_info_result.get("error"):
        extraction_error = str(enhanced_info_result.get("error"))
        logger.error(f"❌ [{source} Candidate:{candidate_id}] LLM error: {extraction_error}")
        formatted_skills = []
    elif enhanced_info_result.get("_cache_hit"):
        # Cached payload already stores skills in the final structured shape
        # ({"skill": ..., "similar_skills": [...]}) under "structured_skills".
        cached_skills = enhanced_info_result.get("structured_skills") or []
        formatted_skills = [
            s for s in cached_skills if isinstance(s, dict) and s.get("skill")
        ]
    else:
        formatted_skills = _normalize_llm_skills(enhanced_info_result)

    company_exp_llm = enhanced_info_result.get("company_experience") or []
    first_exp_title = (
        company_exp_llm[0].get("title")
        if isinstance(company_exp_llm, list) and company_exp_llm and isinstance(company_exp_llm[0], dict)
        else None
    )
    extracted_job_title = (
        enhanced_info_result.get("job_title")
        or enhanced_info_result.get("current_title")
        or first_exp_title
        or fallbacks.get("title")
    )

    llm_name = _clean_extracted_value(enhanced_info_result.get("candidate_name"))
    fallback_name = _clean_extracted_value(fallbacks.get("name"))
    final_name = llm_name or fallback_name or None
    logger.info(f"👤 [{source} Candidate:{candidate_id}] Name: LLM='{llm_name}', Fallback='{fallback_name}', Final='{final_name}'")

    enhanced_info = {
        "candidate_name": final_name,
        "email": _clean_extracted_value(enhanced_info_result.get("email")) or _clean_extracted_value(fallbacks.get("email")),
        "phone": _clean_extracted_value(enhanced_info_result.get("phone")) or _clean_extracted_value(fallbacks.get("phone")),
        "job_title": _clean_extracted_value(extracted_job_title),
        "years_of_experience": enhanced_info_result.get("years_of_experience"),
        "current_location": _clean_extracted_value(enhanced_info_result.get("current_location")) or _clean_extracted_value(fallbacks.get("location")),

        "company_experience": company_exp_llm or fallbacks.get("company_experience", []),
        "candidate_education": enhanced_info_result.get("candidate_education", []) or fallbacks.get("education", []),
        "candidate_certification": enhanced_info_result.get("candidate_certification", []) or fallbacks.get("certifications", []),
        "urls": _normalize_candidate_urls({
            **(fallbacks.get("urls") or {}),
            **(enhanced_info_result.get("urls") or {}),
        }),
        "structured_skills": formatted_skills or fallbacks.get("skills", []),

        "source": source,
        "resume_extraction_status": "completed" if any([
            final_name,
            _clean_extracted_value(extracted_job_title),
            formatted_skills,
            company_exp_llm,
            enhanced_info_result.get("candidate_education", []),
            _clean_extracted_value(enhanced_info_result.get("email")),
            _clean_extracted_value(enhanced_info_result.get("current_location")),
        ]) else "partial"
    }

    # Fix 2 (Path A′ observability): stamp LLM-extraction error onto the
    # enhanced_info so downstream scoring and the streamed stage event can
    # tell a silent degradation apart from a clean empty extraction.
    if extraction_error:
        enhanced_info["_extraction_error"] = extraction_error

    _log_extraction_snapshot(candidate_id, enhanced_info)

    try:
        save_candidate_enhanced_info(candidate_id, enhanced_info, resume_text_to_save)
        logger.info(f"✅ [{source} Candidate:{candidate_id}] Saved to candidate_enhanced_info")
    except Exception as e:
        logger.error(f"❌ [{source} Candidate:{candidate_id}] Failed to save: {e}")

    logger.info("[%sExtract] candidate_id=%s persisted=yes source=%s", source, candidate_id, source)

    return {
        "candidate_id": candidate_id,
        "name": enhanced_info.get("candidate_name"),
        "current_title": enhanced_info.get("job_title"),
        "location": enhanced_info.get("current_location"),
        "years_experience": enhanced_info.get("years_of_experience"),
        "skills": formatted_skills,
        "company_experience": enhanced_info.get("company_experience", []),
        "education": enhanced_info.get("candidate_education", []),
        "certifications": enhanced_info.get("candidate_certification", []),
        "urls": enhanced_info.get("urls", {}),
        "raw": enhanced_info,
        "_extraction_error": extraction_error,
    }


async def process_jobdiva_candidate(candidate: Dict[str, Any]):
    candidate_id = candidate.get("candidate_id", "unknown")
    original_resume_text = candidate.get("resume_text", "")
    if not _has_real_resume_text(original_resume_text):
        logger.warning(f"⚠️ [Candidate:{candidate_id}] No usable resume text found, skipping processing")
        return {"candidate_id": candidate_id, "raw": {}, "skipped": True}

    resume_contact_fallbacks = _extract_resume_contact_details(original_resume_text)
    fallbacks = {
        "name": candidate.get("name"),
        "email": resume_contact_fallbacks.get("email") or candidate.get("email"),
        "phone": resume_contact_fallbacks.get("phone") or candidate.get("phone"),
        "location": candidate.get("location"),
        "title": candidate.get("title") or candidate.get("headline"),
        "urls": resume_contact_fallbacks.get("urls") or {},
    }
    return await _process_candidate_common(
        candidate,
        resume_text_for_llm=original_resume_text,
        resume_text_to_save=original_resume_text,
        source=candidate.get("source", "JobDiva"),
        fallbacks=fallbacks,
    )


async def process_linkedin_candidate(candidate: Dict[str, Any]):
    """Process LinkedIn candidate and save enhanced info to database."""
    candidate_name = candidate.get("name", "Unknown")
    profile_summary = candidate.get("summary", "")
    headline = candidate.get("title", candidate.get("headline", ""))
    location = candidate.get("location", candidate.get("city", ""))
    company_exp = candidate.get("company_experience", [])
    education = candidate.get("candidate_education", [])
    certifications = candidate.get("candidate_certification", [])
    skills = candidate.get("skills", [])
    profile_url = candidate.get("profile_url", "")

    linkedin_profile_text = f"""
Name: {candidate_name}
Headline: {headline}
Location: {location}
Profile URL: {profile_url}

Summary:
{profile_summary}

Experience:
"""
    for exp in company_exp:
        linkedin_profile_text += f"- {exp.get('title', '')} at {exp.get('company', '')} ({exp.get('start_date', '')} to {exp.get('end_date', '')})\n"
    linkedin_profile_text += "\nEducation:\n"
    for edu in education:
        linkedin_profile_text += f"- {edu.get('degree', '')} from {edu.get('institution', '')} ({edu.get('year', '')})\n"
    linkedin_profile_text += "\nCertifications:\n"
    for cert in certifications:
        linkedin_profile_text += f"- {cert.get('name', '')} by {cert.get('issuer', '')} ({cert.get('year', '')})\n"
    linkedin_profile_text += "\nSkills:\n"
    for skill in skills:
        skill_name = skill.get("name", skill) if isinstance(skill, dict) else skill
        linkedin_profile_text += f"- {skill_name}\n"

    fallbacks = {
        "name": candidate_name,
        "email": candidate.get("email"),
        "phone": candidate.get("phone"),
        "location": location,
        "title": headline,
        "urls": {"linkedin": profile_url} if profile_url else {},
        "skills": skills,
        "company_experience": company_exp,
        "education": education,
        "certifications": certifications,
    }
    return await _process_candidate_common(
        candidate,
        resume_text_for_llm=linkedin_profile_text,
        resume_text_to_save=linkedin_profile_text,
        source="LinkedIn",
        fallbacks=fallbacks,
        min_text_length=100,
    )


async def process_dice_candidate(candidate: Dict[str, Any]):
    """Process Dice candidate and save enhanced info to database."""
    candidate_name = candidate.get("name", "Unknown")
    headline = candidate.get("title", candidate.get("headline", ""))
    location = candidate.get("location", candidate.get("city", ""))
    resume_text = candidate.get("resume_text", "")

    if not _has_real_resume_text(resume_text):
        resume_text = f"\nName: {candidate_name}\nTitle: {headline}\nLocation: {location}\n"

    fallbacks = {
        "name": candidate_name,
        "email": candidate.get("email"),
        "phone": candidate.get("phone"),
        "location": location,
        "title": headline,
    }
    return await _process_candidate_common(
        candidate,
        resume_text_for_llm=resume_text,
        resume_text_to_save=resume_text,
        source="Dice",
        fallbacks=fallbacks,
    )

# --- Save Functions ---
def save_candidate_enhanced_info(candidate_id: str, enhanced_info: Dict[str, Any], resume_text: str):
    """Save or update candidate_enhanced_info table with enriched data."""
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            # v22: DDL moved to lifespan (`init_sourced_candidates_schema`).
            # Previously `CREATE TABLE IF NOT EXISTS` + two `ALTER TABLE` +
            # `CREATE INDEX` ran per save → ACCESS EXCLUSIVE lock contention
            # under concurrent writes. Save path is pure DML now.

            # Safe int parsing for years_experience
            raw_years = enhanced_info.get("years_of_experience")
            parsed_years = None
            if isinstance(raw_years, int) or isinstance(raw_years, float):
                parsed_years = int(raw_years)
            elif isinstance(raw_years, str):
                import re
                match = re.search(r'\d+', raw_years)
                if match:
                    parsed_years = int(match.group())
            
            # Extract data from enhanced_info
            company_exp = enhanced_info.get("company_experience", [])
            education = enhanced_info.get("candidate_education", [])
            certifications = enhanced_info.get("candidate_certification", [])
            urls = enhanced_info.get("urls", {})
            
            resume_hash = _resume_text_hash(resume_text or "")

            conn.execute(text("""
                INSERT INTO candidate_enhanced_info
                (candidate_id, candidate_name, email, phone, job_title, current_location,
                 years_of_experience, key_skills, company_experience, candidate_education,
                 candidate_certification, urls, resume_text, resume_hash,
                 resume_extraction_status, source, extracted_at)
                VALUES (:candidate_id, :candidate_name, :email, :phone, :job_title, :current_location,
                        :years_of_experience, :key_skills, :company_experience, :candidate_education,
                        :candidate_certification, :urls, :resume_text, :resume_hash,
                        :resume_extraction_status, :source, CURRENT_TIMESTAMP)
                ON CONFLICT (candidate_id) DO UPDATE SET
                    candidate_name = EXCLUDED.candidate_name,
                    email = EXCLUDED.email,
                    phone = EXCLUDED.phone,
                    job_title = EXCLUDED.job_title,
                    current_location = EXCLUDED.current_location,
                    years_of_experience = EXCLUDED.years_of_experience,
                    key_skills = EXCLUDED.key_skills,
                    company_experience = EXCLUDED.company_experience,
                    candidate_education = EXCLUDED.candidate_education,
                    candidate_certification = EXCLUDED.candidate_certification,
                    urls = EXCLUDED.urls,
                    resume_text = EXCLUDED.resume_text,
                    resume_hash = EXCLUDED.resume_hash,
                    resume_extraction_status = EXCLUDED.resume_extraction_status,
                    source = EXCLUDED.source,
                    extracted_at = CURRENT_TIMESTAMP
            """), {
                "candidate_id": candidate_id,
                "candidate_name": enhanced_info.get("candidate_name"),
                "email": enhanced_info.get("email"),
                "phone": enhanced_info.get("phone"),
                "job_title": enhanced_info.get("job_title"),
                "current_location": enhanced_info.get("current_location"),
                "years_of_experience": parsed_years,
                "key_skills": json.dumps(enhanced_info.get("structured_skills", [])),
                "company_experience": json.dumps(company_exp),
                "candidate_education": json.dumps(education),
                "candidate_certification": json.dumps(certifications),
                "urls": json.dumps(urls),
                "resume_text": resume_text,
                "resume_hash": resume_hash,
                "resume_extraction_status": enhanced_info.get("resume_extraction_status", "pending"),
                "source": enhanced_info.get("source", "JobDiva")
            })
            conn.commit()
            
            # --- Propagate strictly to the newly normalized relations ---
            try:
                candidate_profiles_db.upsert_candidate(
                    jobdiva_id="", # We may not have jobdiva_id locally here but it operates as intended
                    candidate=enhanced_info,
                    source=enhanced_info.get("source", "JobDiva")
                )
            except Exception as e_norm:
                logger.error(f"❌ Normalized persistence failed for {candidate_id}: {e_norm}")
    except Exception as e:
        print(f"Error saving candidate_enhanced_info for {candidate_id}: {e}")

def save_sourced_candidate(candidate: Dict[str, Any], enhanced_info: Dict[str, Any]):
    """Update sourced_candidates table with enriched LLM-only candidate info."""
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            sourced_payload = {
                "candidate_name": enhanced_info.get("candidate_name") or candidate.get("name"),
                "email": enhanced_info.get("email") or candidate.get("email"),
                "phone": enhanced_info.get("phone") or candidate.get("phone"),
                "job_title": enhanced_info.get("job_title") or candidate.get("title") or candidate.get("headline"),
                "years_of_experience": enhanced_info.get("years_of_experience"),
                "current_location": enhanced_info.get("current_location") or candidate.get("location"),
                "skills": enhanced_info.get("structured_skills", []),
                "company_experience": enhanced_info.get("company_experience", []),
                "candidate_education": enhanced_info.get("candidate_education", []),
                "candidate_certification": enhanced_info.get("candidate_certification", []),
                "urls": enhanced_info.get("urls", {}),
                "resume_text": candidate.get("resume_text", ""),
                "resume_extraction_status": enhanced_info.get("resume_extraction_status", "pending"),
                "source": enhanced_info.get("source", candidate.get("source", "JobDiva"))
            }

            conn.execute(text("""
                UPDATE sourced_candidates SET
                    name = COALESCE(:name, name),
                    email = COALESCE(:email, email),
                    phone = COALESCE(:phone, phone),
                    headline = COALESCE(:headline, headline),
                    location = COALESCE(:location, location),
                    resume_text = COALESCE(:resume_text, resume_text),
                    data = :data,
                    updated_at = CURRENT_TIMESTAMP
                WHERE jobdiva_id = :jobdiva_id AND candidate_id = :candidate_id AND source = :source
            """), {
                "name": sourced_payload["candidate_name"],
                "email": sourced_payload["email"],
                "phone": sourced_payload["phone"],
                "headline": sourced_payload["job_title"],
                "location": sourced_payload["current_location"],
                "resume_text": sourced_payload["resume_text"],
                "data": json.dumps(sourced_payload),
                "jobdiva_id": candidate.get("jobdiva_id"),
                "candidate_id": candidate["candidate_id"],
                "source": candidate.get("source", "JobDiva")
            })
            conn.commit()
    except Exception as e:
        print(f"Error updating sourced_candidates for {candidate['candidate_id']}: {e}")

# Implement save_candidate_enhanced_info and save_sourced_candidate as needed
