import logging
import sqlalchemy
from sqlalchemy import text
from typing import List, Dict, Any
from utils.crypto import decrypt_field
from core.config import DATABASE_URL

logger = logging.getLogger(__name__)

class VettedService:
    """
    Service to search candidates in the AWS RDS 'Vetted' database.
    Schema is normalized:
    - Candidate (id, name, email, city (FK), state (FK), year_of_experience)
    - CandidateFieldCity (id, city)
    - CandidatesSkills (candidateId, skillFinal (FK int), yearsOfExperience)
    - CandidatesSkillFieldSkillFinal (id, skillFinal (text))
    """

    def __init__(self):
        self.db_url = DATABASE_URL
            
        self.engine = None
        if self.db_url:
            try:
                # v22: add pool sizing + pre_ping + connect_timeout.
                self.engine = sqlalchemy.create_engine(
                    self.db_url,
                    pool_size=5,
                    max_overflow=10,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    connect_args={"connect_timeout": 5},
                )
            except Exception as e:
                logger.error(f"Failed to create Vetted DB engine: {e}")

    async def search_candidates(self, skills: List[str], location: str = None, page: int = 1, limit: int = 20) -> List[Dict[str, Any]]:
        results = []
        if not self.engine:
            logger.warning("Vetted DB Engine not initialized. Returning empty.")
            # DEBUG: Return error for engine failure
            return [{
                "id": "error-engine",
                "firstName": "ERROR: ENGINE IS NONE", 
                "resume_text": f"DEBUG: DATABASE_URL is '{self.db_url if self.db_url else 'NONE'}'"
            }]

        try:
            conn = self.engine.connect()
            
            # DEBUG: Diagnostic Check
            try:
                diag_res = conn.execute(text('SELECT COUNT(*) FROM "Candidate"'))
                count = diag_res.scalar()
                if count == 0:
                     conn.close()
                     return [{"id": "debug-empty", "firstName": "DEBUG: DB IS EMPTY", "resume_text": "Zero rows in Candidate table"}]
                # If we have rows, maybe filters are wrong? Let's return Total Count for now
                if skills and "DEBUG_COUNT" in skills:
                     conn.close()
                     return [{"id": "debug-count", "firstName": f"DEBUG: TOTAL ROWS {count}", "resume_text": f"Connected to {self.db_url}"}]
            except Exception as e:
                conn.close()
                return [{"id": "debug-error", "firstName": f"DEBUG ERROR: {e}"}]
            
            # Base Query with Joins
            # We select candidate details and standardizing output
            # Base Query with Joins and Aggregation for Skills to avoid N+1
            # We select candidate details and standardizing output
            # Group by candidate to aggregate skills into a list
            query_str = """
                SELECT 
                    c.id, 
                    c.name, 
                    c.email, 
                    c.year_of_experience,
                    cfc.city,
                    c."linkedIn_link" as linkedin,
                    string_agg(DISTINCT sf."skillFinal", ', ') as skill_list
                FROM "Candidate" c
                LEFT JOIN "CandidateFieldCity" cfc ON c.city = cfc.id
                LEFT JOIN "CandidatesSkills" cs ON c.id = cs."candidateId"
                LEFT JOIN "CandidatesSkillFieldSkillFinal" sf ON cs."skillFinal" = sf.id
                WHERE c."optedOut" IS NOT TRUE
            """
            
            params = {}
            
            # Location Filter (City Name check on joined table)
            if location:
                city_only = location.split(',')[0].strip()
                query_str += " AND cfc.city ILIKE :location"
                params["location"] = f"%{city_only}%"
            
            # Skills Filter (EXISTS clause is efficient for filtering)
            if skills:
                clean_skills = [s.strip() for s in skills if s]
                if clean_skills:
                    # Dynamically build OR clauses for ILIKE
                    skill_conditions = []
                    for idx, skill in enumerate(clean_skills):
                        param_key = f"skill_{idx}"
                        skill_conditions.append(f'sf_sub."skillFinal" ILIKE :{param_key}')
                        params[param_key] = f"%{skill}%"
                    
                    or_clause = " OR ".join(skill_conditions)
                    
                    query_str += f"""
                        AND EXISTS (
                            SELECT 1 
                            FROM "CandidatesSkills" cs_sub 
                            JOIN "CandidatesSkillFieldSkillFinal" sf_sub ON cs_sub."skillFinal" = sf_sub.id
                            WHERE cs_sub."candidateId" = c.id 
                            AND ({or_clause})
                        )
                    """

            # Grouping for aggregation
            query_str += """
                GROUP BY c.id, c.name, c.email, c.year_of_experience, cfc.city, c."linkedIn_link"
            """

            # Pagination
            offset = (page - 1) * limit
            query_str += " LIMIT :limit OFFSET :offset"
            params["limit"] = limit
            params["offset"] = offset
            
            print(f"🔍 VETTED SQL: {query_str}")
            print(f"🔍 VETTED PARAMS: {params}")
            
            result = conn.execute(text(query_str), params)
            rows = result.fetchall()
            print(f"🔍 VETTED: Query returned {len(rows)} rows")
            
            # DEBUG: Add query info to first result if no rows
            if len(rows) == 0:
                conn.close()
                return [{
                    "id": "debug-no-results",
                    "firstName": "NO RESULTS FROM QUERY",
                    "lastName": f"Skills: {skills}",
                    "email": f"Params sent: {list(params.keys())}",
                    "source": "VettedDB",
                    "match_score": 0,
                    "skills": [],
                    "experience": 0
                }]
            
            # Process Rows
            for row in rows:
                full_name = decrypt_field(row.name) if row.name else ""
                email = decrypt_field(row.email) if row.email else ""
                
                # Split Name
                parts = full_name.split(" ", 1)
                fname = parts[0]
                lname = parts[1] if len(parts) > 1 else ""
                
                # Parse aggregated skills
                cand_skills = row.skill_list.split(', ') if row.skill_list else []
                # Limit to 10 for display
                cand_skills = cand_skills[:10]

                results.append({
                    "id": str(row.id),
                    "firstName": fname,
                    "lastName": lname,
                    "email": email,
                    "city": row.city,
                    "state": "",
                    "source": "VettedDB",
                    "match_score": 95, 
                    "skills": cand_skills,
                    "experience": row.year_of_experience,
                    "resume_text": f"Vetted candidate. LinkedIn: {row.linkedin}. Skills: {', '.join(cand_skills)}"
                })

            conn.close()
            return results
                
        except Exception as e:
            logger.error(f"Vetted Search Failed: {e}")
            if 'conn' in locals(): conn.close()
            # DEBUG: Return error as candidate
            return [{
                "id": "error",
                "firstName": f"ERROR: {str(e)}",
                "lastName": "DEBUG",
                "email": "error@debug.com",
                "city": "ErrorCity",
                "state": "ES",
                "source": "VettedDB",
                "match_score": 0, 
                "skills": ["Error"],
                "experience": 0,
                "resume_text": f"FULL ERROR: {str(e)}"
            }]
            # return []

    async def get_candidate_resume(self, candidate_id: str) -> str:
        """
        Fetches the 'resume' (summary of skills/linkedin) for a candidate by ID.
        Since Vetted DB does not assume full resume text storage, we construct it.
        """
        if not self.engine:
            return "Error: Database Engine not initialized."

        try:
            conn = self.engine.connect()
            
            # Simple query by ID
            query_str = """
                SELECT 
                    c.name, 
                    c.email, 
                    c."linkedIn_link" as linkedin,
                    string_agg(DISTINCT sf."skillFinal", ', ') as skill_list
                FROM "Candidate" c
                LEFT JOIN "CandidatesSkills" cs ON c.id = cs."candidateId"
                LEFT JOIN "CandidatesSkillFieldSkillFinal" sf ON cs."skillFinal" = sf.id
                WHERE c.id = :candidate_id
                GROUP BY c.id, c.name, c.email, c."linkedIn_link"
            """
            
            # Handle string vs int id
            # Assuming ID is INT in DB
            try:
                cid = int(candidate_id)
            except:
                cid = candidate_id # fallback
                
            result = conn.execute(text(query_str), {"candidate_id": cid})
            row = result.fetchone()
            conn.close()
            
            if not row:
                return ""
            
            # Construct text
            # Note: We decrypt name just in case it's needed in future, but resume text usually anonymized?
            # actually we return "Vetted candidate..." as extracted text.
            
            cand_skills = row.skill_list if row.skill_list else "None"
            linkedin = row.linkedin if row.linkedin else "None"
            
            return f"Vetted candidate. LinkedIn: {linkedin}. Skills: {cand_skills}"
            
        except Exception as e:
            logger.error(f"Vetted Resume Fetch Failed: {e}")
            if 'conn' in locals(): conn.close()
            return f"Error fetching resume: {e}"

vetted_service = VettedService()
