#!/usr/bin/env python3
"""
Job Skills Extractor Service
Integrates with Ronak's Skills Ontology to extract and normalize job skills
"""

from typing import List, Dict, Optional, Tuple
import re
import json
import logging
from datetime import datetime
from dataclasses import dataclass
from core.graph import ontology  # Ronak's skills system
import openai
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# Taxonomy-grounded extraction service
try:
    from services.taxonomy_service import extract_grounded_rubric
    TAXONOMY_GROUNDING_AVAILABLE = True
    print("✅ Taxonomy grounding service loaded successfully")
except Exception as _tax_import_err:
    TAXONOMY_GROUNDING_AVAILABLE = False
    print(f"⚠️  Taxonomy grounding NOT available: {_tax_import_err}")

@dataclass
class ExtractedSkill:
    """Represents a skill extracted from job text"""
    original_text: str          # "React.js development"
    normalized_name: str        # "React"
    skill_id: Optional[str]     # "react" (from Ronak's skill_nodes.slug)
    importance: str             # "required", "preferred", "nice_to_have" 
    min_years: int = 0          # Minimum years extracted
    proficiency: Optional[str] = None  # "junior", "senior", "expert"
    confidence: float = 0.0     # AI confidence score
    source_context: str = ""    # Where in job description this was found
    category: str = "hard"      # "hard" or "soft"

@dataclass
class JobRubric:
    """Full rubric model for candidate assessment"""
    titles: List[Dict]
    skills: List[Dict]
    education: List[Dict]
    domain: List[Dict]
    customer_requirements: List[Dict]
    other_requirements: List[Dict]
    soft_skills: List[Dict] = None

@dataclass
class JobSkillsAnalysis:
    """Complete skills analysis for a job"""
    job_id: str
    extracted_skills: List[ExtractedSkill]
    unmapped_skills: List[str]   # Skills not found in Ronak's ontology
    analysis_metadata: Dict
    
class JobSkillsExtractor:
    """Extracts skills from job descriptions and maps them to Ronak's ontology"""
    
    def __init__(self, openai_api_key: str):
        self.openai_client = openai.AsyncOpenAI(api_key=openai_api_key)
        self.ontology = ontology  # Ronak's skills ontology
    
    async def analyze_job_skills(
        self, 
        job_id: str,
        jobdiva_description: str = "",
        ai_description: str = "", 
        recruiter_notes: str = ""
    ) -> JobSkillsAnalysis:
        """
        Analyzes job content and extracts skills mapped to Ronak's ontology
        """
        print(f"🔍 Analyzing skills for job {job_id}")
        
        # Combine all text sources
        combined_text = self._combine_job_texts(
            jobdiva_description, ai_description, recruiter_notes
        )
        
        # Extract skills using AI
        raw_skills = await self._extract_skills_with_ai(combined_text)
        
        # Map to Ronak's ontology  
        mapped_skills = self._map_skills_to_ontology(raw_skills)
        
        # Create analysis result
        analysis = JobSkillsAnalysis(
            job_id=job_id,
            extracted_skills=mapped_skills['mapped'],
            unmapped_skills=mapped_skills['unmapped'],
            analysis_metadata={
                'total_skills_found': len(raw_skills),
                'mapped_skills': len(mapped_skills['mapped']),
                'unmapped_skills': len(mapped_skills['unmapped']),
                'mapping_rate': len(mapped_skills['mapped']) / len(raw_skills) if raw_skills else 0,
                'analysis_timestamp': datetime.now().isoformat()
            }
        )
        
        print(f"✅ Found {len(analysis.extracted_skills)} mapped skills")
        return analysis
    
    def _combine_job_texts(self, jobdiva: str, ai: str, notes: str) -> str:
        """Combines all job text sources for analysis"""
        sections = []
        
        if jobdiva:
            sections.append(f"JobDiva Description:\n{jobdiva}")
        if ai: 
            sections.append(f"Enhanced Description:\n{ai}")
        if notes:
            sections.append(f"Recruiter Notes:\n{notes}")
            
        return "\n\n---\n\n".join(sections)
    
    async def _extract_skills_with_ai(self, job_text: str) -> List[Dict]:
        """Uses OpenAI to extract skills from job text"""
        
        prompt = f"""
        Analyze the following job description and extract ALL technical skills, tools, and technologies mentioned.
        For each skill, provide:
        1. The exact text as mentioned in the job
        2. A normalized skill name (e.g., "React.js" → "React")
        3. Importance level: required, preferred, or nice_to_have
        4. Minimum years of experience if mentioned
        5. Proficiency level if mentioned (junior, mid, senior, expert)
        
        Focus on:
        - Programming languages (Python, Java, JavaScript, etc.)
        - Frameworks and libraries (React, Angular, Django, etc.) 
        - Tools and platforms (Docker, AWS, Git, etc.)
        - Databases (PostgreSQL, MongoDB, etc.)
        - Methodologies (Agile, DevOps, etc.)
        
        Return as JSON array with format:
        {{
            "original_text": "3+ years React.js experience", 
            "normalized_name": "React",
            "importance": "required",
            "min_years": 3,
            "proficiency": null,
            "confidence": 0.95
        }}
        
        Job Description:
        {job_text}
        """
        
        try:
            response = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an expert at extracting technical skills from job descriptions."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            
            # Parse JSON response
            content = response.choices[0].message.content
            # Extract JSON from response (handle markdown formatting) 
            if "```json" in content:
                json_start = content.find("```json") + 7
                json_end = content.find("```", json_start)
                content = content[json_start:json_end]
            
            skills_data = json.loads(content)
            return skills_data
            
        except Exception as e:
            print(f"❌ AI extraction failed: {e}")
            return []
    
    def _map_skills_to_ontology(self, raw_skills: List[Dict]) -> Dict[str, List]:
        """Maps extracted skills to Ronak's skills ontology"""
        mapped_skills = []
        unmapped_skills = []
        
        for skill_data in raw_skills:
            normalized_name = skill_data.get('normalized_name', '')
            
            # Try to find skill in Ronak's ontology
            skill_id = self._find_skill_in_ontology(normalized_name)
            
            if skill_id:
                # Found in ontology - create mapped skill
                extracted_skill = ExtractedSkill(
                    original_text=skill_data.get('original_text', ''),
                    normalized_name=normalized_name,
                    skill_id=skill_id,
                    importance=skill_data.get('importance', 'preferred'),
                    min_years=skill_data.get('min_years', 0),
                    proficiency=skill_data.get('proficiency'),
                    confidence=skill_data.get('confidence', 0.0)
                )
                mapped_skills.append(extracted_skill)
            else:
                # Not found in ontology
                unmapped_skills.append(normalized_name)
                print(f"⚠️ Skill not found in ontology: {normalized_name}")
        
        return {
            'mapped': mapped_skills,
            'unmapped': unmapped_skills
        }
    
    def _find_skill_in_ontology(self, skill_name: str) -> Optional[str]:
        """
        Finds skill in Ronak's ontology using multiple strategies
        Returns skill_id (slug) if found
        """
        # Strategy 1: Direct lookup by normalized name
        skill_id = ontology.resolve_alias(skill_name)
        if skill_id != skill_name:  # Found alias mapping
            return skill_id
            
        # Strategy 2: Check if skill_name exists as a node
        if skill_name.lower() in ontology.graph.nodes:
            return skill_name.lower()
        
        # Strategy 3: Fuzzy matching (basic implementation)
        # Could be enhanced with more sophisticated matching
        for node in ontology.graph.nodes:
            if skill_name.lower() in node or node in skill_name.lower():
                return node
                
        return None

    def _normalize_title_key(self, title: str) -> str:
        """Normalizes a title to a pure alphanumeric string for robust comparison."""
        if not title:
            return ""
        return re.sub(r'[^a-z0-9]', '', title.lower())

    async def extract_full_rubric(
        self,
        job_id: str,
        job_title: str,
        enhanced_job_title: str = "",
        jobdiva_description: str = "",
        ai_description: str = "",
        recruiter_notes: str = "",
        customer_name: str = "",
        job_location: str = "",
        location_type: str = ""
    ) -> JobRubric:
        """
        Extracts a complete rubric (titles, skills, education, etc.) strictly from job text.
        NO MOCK DATA - everything must come from the provided sources.
        """
        combined_text = self._combine_job_texts(jobdiva_description, ai_description, recruiter_notes)
        
        prompt = f"""
        Analyze the following job details and extract a structured assessment rubric.
        YOU MUST ONLY EXTRACT FACTS PRESENT IN THE TEXT. DO NOT INVENT REQUIREMENTS.
        
        FORMATTING RULE: Every extracted value (titles, skills, degrees, etc.) MUST be in Title Case (e.g. "Radiographic Positioning", NOT "radiographic positioning").
        
        STRICT EXTRACTION PRIORITY:
        1. Recruiter Notes (highest authority)
        2. AI Enhanced Job Description
        3. Original JobDiva Description (may contain HTML, please ignore tags and extract text facts)
        
        Extract the following components:
        
        1. TITLES:
           - Extract any acceptable alternative job titles mentioned in the text.
           - For each, provide: value, minYears, recent (boolean), matchType (ALWAYS return "Similar"), required ("Required" or "Preferred").
        
        2. SKILLS (Measurable Hard Skills Only):
           - Extract the **top 8 most critical** technical skills, tools, or methodologies.
           - PRIORITIZATION: If more than 8 skills are found, prioritize the most essential ones for the role and group related tools if possible (e.g., "Adobe Creative Suite" instead of separate entries) to maximize the value of each slot.
           - STRICT EXCLUSION: DO NOT extract soft skills (e.g., "Communication", "Teamwork", "Leadership", "Problem Solving").
           - STRICT EXCLUSION: DO NOT extract certifications or licenses (e.g., "ARRT", "BLS", "ACLS"). These go into EDUCATION.
           - For each: value, minYears, recent (boolean), matchType (ALWAYS return "Similar"), required ("Required" or "Preferred").
        
        3. EDUCATION & CERTIFICATIONS:
           - Extract ONLY specific academic degrees and professional certifications mentioned in the text.
           - degree: Choose ONLY from ["High School / GED", "Associate's degree", "Bachelor's degree", "Master's degree", "PhD or equivalent", "Certification / License"].
           - MAPPING RULES: 
             - Use "Certification / License" for any professional certification (e.g. "CPR", "ARRT").
             - **STRICT EXCLUSION**: DO NOT invent "No requirement" rows. If no specific academic degree is mentioned beyond a required license/certification, ONLY return the license/certification.
           - field: Field of study or Certification name (e.g., "Radiologic Technology" or "Basic Life Support").
           - required: "Required" or "Preferred".
        
        4. DOMAIN:
           - Industry or domain experience required (e.g., "Healthcare", "Fintech").
           - value, required ("Required" or "Preferred").
        
        5. CUSTOMER REQUIREMENTS:
           - type: Choose ONLY from ["Must not be employed by", "Currently employed by", "Previously employed by"].
           - value: Specific detail if mentioned.
        
        6. OTHER REQUIREMENTS:
           - Location constraints, shift requirements, and specific physical or schedule needs.
           - value, required ("Required" or "Preferred").
        
        Input Context:
        Job Title: {job_title}
        Customer: {customer_name}
        {combined_text}
        
        Return ONLY a JSON object with this structure:
        {{
            "titles": [{{ "value": "Alternative Title", "minYears": 3, "recent": false, "matchType": "Similar", "required": "Preferred" }}],
            "skills": [{{ "value": "Python", "minYears": 3, "recent": false, "matchType": "Similar", "required": "Required" }}],
            "education": [{{ "degree": "Bachelor's degree", "field": "Computer Science", "required": "Required" }}],
            "domain": [{{ "value": "Healthcare", "required": "Preferred" }}],
            "customer_requirements": [],
            "other_requirements": [{{ "value": "On-call availability required", "required": "Required" }}]
        }}
        """
        
        try:
            # ── Two-pass taxonomy-grounded extraction for Skills & Titles ──────
            grounded_skills         = []
            grounded_soft_skills    = []
            grounded_extra_titles   = []
            grounded_certifications = []
            grounded_domains        = []


            # Use the cleaner AI-generated description (summary) for grounding if available
            grounding_source = ai_description if ai_description and len(ai_description) > 100 else combined_text

            if TAXONOMY_GROUNDING_AVAILABLE:
                try:
                    grounded = await extract_grounded_rubric(
                        job_text=grounding_source,
                        job_title=job_title,
                        client=self.openai_client,
                        max_skills=15,
                        max_titles=4,
                    )
                    grounded_skills         = grounded.get("hard_skills", [])
                    grounded_soft_skills    = grounded.get("soft_skills", [])
                    grounded_extra_titles   = grounded.get("extra_titles", [])
                    grounded_certifications = grounded.get("raw_certifications", [])
                    grounded_domains        = grounded.get("raw_domains", [])

                    logger.info(f"✅ Grounding: {len(grounded_skills)} hard, {len(grounded_soft_skills)} soft, "
                                f"{len(grounded_extra_titles)} titles, {len(grounded_certifications)} certs, "
                                f"{len(grounded_domains)} domains")
                except Exception as tax_err:
                    logger.warning(f"⚠️  Taxonomy grounding failed, falling back to base LLM: {tax_err}")

            # ── Base LLM call — Education, Domain, Customer/Other requirements ─
            response = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a strict recruitment extraction engine. You only extract facts present in the text. You handle technical and medical roles with high precision."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                response_format={ "type": "json_object" }
            )

            rubric_data = json.loads(response.choices[0].message.content)

            # Inject grounded skills (overrides base LLM if taxonomy succeeded)
            if grounded_skills or grounded_soft_skills:
                # Keep strictly separate: UI only maps over `skills` (hard), 
                # but JS state preserves `soft_skills` and automatically posts it to the DB.
                rubric_data["skills"] = grounded_skills
                rubric_data["soft_skills"] = grounded_soft_skills
            else:
                # Fallback path (Legacy LLM) - default to hard
                for s in rubric_data.get("skills", []):
                    s["category"] = "hard"
            
            # 1. Start with the JobDiva title (Master)
            final_titles = [{
                "value": job_title,
                "minYears": 0,
                "recent": False,
                "matchType": "Similar",
                "required": "Required",
                "source": "PAIR"
            }]
            seen_keys = {self._normalize_title_key(job_title)}

            # 2. Inject enhanced title ONLY if recruiter clicked Enhance
            # and it is genuinely different from the original title
            if enhanced_job_title and enhanced_job_title.strip():
                enh_key = self._normalize_title_key(enhanced_job_title)
                if enh_key not in seen_keys:
                    final_titles.append({
                        "value": enhanced_job_title.strip(),
                        "minYears": 0,
                        "recent": False,
                        "matchType": "Similar",
                        "required": "Preferred",
                        "source": "PAIR"
                    })
                    seen_keys.add(enh_key)
            
            rubric_data['titles'] = final_titles

            # Enforce matchType=Similar on all skills (taxonomy grounding already normalized names)
            for s in rubric_data.get('skills', []):
                s['matchType'] = 'Similar'

            deduped_titles = rubric_data['titles']

            # ── Route grounded certifications → education ──────────────────────
            if grounded_certifications:
                cert_entries = [
                    {"degree": "Certification / License", "field": c, "required": "Required"}
                    for c in grounded_certifications
                ]
                existing = rubric_data.get('education', [])
                
                # Smart merge: fuzzy deduplicate to avoid "BLS Certification" vs "Basic Life Support (BLS)"
                for edu in existing:
                    field_name = edu.get('field', '')
                    degree_name = edu.get('degree', '')
                    
                    is_duplicate = False
                    if degree_name == "Certification / License" and field_name:
                        for existing_cert in cert_entries:
                            score = fuzz.token_set_ratio(field_name.upper(), existing_cert['field'].upper())
                            if score > 80:
                                is_duplicate = True
                                break
                                
                    if not is_duplicate:
                        cert_entries.append(edu)
                        
                rubric_data['education'] = cert_entries
                logger.info(f"   🎓 Certifications routed to education ({len(cert_entries)}):")
                for c in cert_entries:
                    # Log the actual cert name if available, else the degree type
                    logger.info(f"      - {c.get('field') or c.get('degree')}")
            elif not rubric_data.get('education'):
                # Fallback default
                is_medical = any(w in job_title.lower() for w in ['tech', 'technician', 'nurse', 'doctor', 'medical', 'health', 'radiologic'])
                rubric_data['education'] = [{
                    "degree": "Certification / License" if is_medical else "Bachelor's degree",
                    "field": "Relevant field",
                    "required": "Preferred"
                }]

            # ── Route grounded domains → domain section ────────────────────────
            if grounded_domains:
                domain_entries = [{"value": d, "required": "Preferred"} for d in grounded_domains]
                existing_domains = rubric_data.get('domain', [])
                seen_domains = {d['value'].upper() for d in domain_entries}
                for d in existing_domains:
                    if d.get('value', '').upper() not in seen_domains:
                        domain_entries.append(d)
                rubric_data['domain'] = domain_entries
                logger.info(f"   🌐 Domains routed to domain section ({len(domain_entries)}):")
                for d in domain_entries:
                    logger.info(f"      - {d['value']}")


            # 5. Inject default Customer Requirement if customer_name exists
            if customer_name and customer_name.strip():
                # Check for existing "Must not be employed by" for this customer
                exists = any(
                    req.get('type') == 'Must not be employed by' and 
                    customer_name.lower() in str(req.get('value', '')).lower()
                    for req in rubric_data.get('customer_requirements', [])
                )
                if not exists:
                    rubric_data.setdefault('customer_requirements', []).insert(0, {
                        "type": "Must not be employed by",
                        "value": customer_name,
                        "required": "Required"
                    })

            # 6. Inject default Location Requirement if job_location exists and NOT remote
            if job_location and job_location.strip() and location_type.lower() != 'remote':
                # Check for existing location requirement for this location
                exists = any(
                    'local' in str(req.get('value', '')).lower() and 
                    job_location.split(',')[0].lower() in str(req.get('value', '')).lower()
                    for req in rubric_data.get('other_requirements', [])
                )
                if not exists:
                    rubric_data.setdefault('other_requirements', []).insert(0, {
                        "value": f"Must be local to {job_location} metro",
                        "required": "Required"
                    })

            # Log final results for debugging
            logger.info("─" * 60)
            logger.info(f"✨ FINAL RUBRIC — Job {job_id}")
            logger.info(f"   👔 Roles ({len(rubric_data.get('titles', []))}):")
            for t in rubric_data.get('titles', []):
                logger.info(f"      - {t['value']}")
            logger.info(f"   🛠️  Skills ({len(rubric_data.get('skills', []))}):")
            for s in rubric_data.get('skills', []):
                logger.info(f"      - {s['value']}")
            logger.info("─" * 60)

            return JobRubric(
                titles=deduped_titles,
                skills=rubric_data.get('skills', []),
                soft_skills=rubric_data.get('soft_skills', []),
                education=rubric_data.get('education', []),
                domain=rubric_data.get('domain', []),
                customer_requirements=rubric_data.get('customer_requirements', []),
                other_requirements=rubric_data.get('other_requirements', [])
            )
            
        except Exception as e:
            print(f"❌ Full rubric extraction failed: {e}")
            # Fallback to minimal info from title if everything fails
            return JobRubric(
                titles=[{"value": job_title, "minYears": 0, "recent": False, "matchType": "Similar", "required": "Required"}],
                skills=[],
                soft_skills=[],
                education=[],
                domain=[],
                customer_requirements=[],
                other_requirements=[]
            )

# Example usage function
async def process_job_skills(job_id: str, job_data: dict) -> JobSkillsAnalysis:
    """
    Processes skills for a job and returns analysis
    Called from your main workflow
    """
    from os import getenv
    
    extractor = JobSkillsExtractor(getenv("OPENAI_API_KEY"))
    
    analysis = await extractor.analyze_job_skills(
        job_id=job_id,
        jobdiva_description=job_data.get('jobdiva_description', ''),
        ai_description=job_data.get('ai_description', ''),
        recruiter_notes=job_data.get('recruiter_notes', '')
    )
    
    return analysis