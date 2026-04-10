#!/usr/bin/env python3
"""
azure_agent_service.py
----------------------
Calls the Azure AI Foundry Agent 'skill-role-extractor'.

Flow:
  1. Send the full AI JD text directly to the agent.
  2. Agent reads the JD against its indexed taxonomy files
     (job-role-taxonomy.xlsx + skills-taxonomy-revelio-33k.xlsx).
  3. Returns strict JSON with grounded roles and skills.
  4. We cascade through the hierarchy columns to pick the most-specific name.
  5. NO fallback — if the agent finds no taxonomy match, the item is dropped.
"""

import json
import asyncio
import logging
import re
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# ── Role hierarchy (most specific → broadest) ─────────────────────────────────
ROLE_COLUMNS = [
    "ROLE_K17000", "ROLE_K10000", "ROLE_K5000",
    "ROLE_K1500",  "ROLE_K1000",  "ROLE_K500",
    "ROLE_K150",   "ROLE_K50",    "ROLE_K10",
]

# ── Skill hierarchy (most specific → broadest) ────────────────────────────────
SKILL_COLUMNS = [
    "skill_mapped", "skill_k15000", "skill_k5000",
    "skill_k1500",  "skill_k500",   "skill_k150",
    "skill_k50",    "skill_k15",
]


class AzureAgentService:
    """
    Wraps the Azure AI Foundry 'skill-role-extractor' agent.
    Uses AIProjectClient + openai_client.responses.create() pattern.
    """

    def __init__(
        self,
        project_endpoint: str,
        api_key: str,
        agent_name: str = "skill-role-extractor",
    ):
        self.project_endpoint = project_endpoint
        self.api_key = api_key
        self.agent_name = agent_name

    # ─────────────────────────────────────────────────────────────────────────
    # Public async entry-point
    # ─────────────────────────────────────────────────────────────────────────

    async def extract_roles_and_skills(self, ai_jd: str) -> Dict:
        """
        Sends the full AI JD to the Azure agent and returns raw parsed JSON.
        Raises on failure — NO silent fallback.

        Returns dict with keys: job_roles (list), job_skills (list)
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._call_agent_sync(ai_jd))

    # ─────────────────────────────────────────────────────────────────────────
    # Sync SDK call (runs in thread pool)
    # ─────────────────────────────────────────────────────────────────────────

    def _call_agent_sync(self, ai_jd: str) -> Dict:
        """
        Step 1: Sending the raw AI JD to the Azure Agent.
        """
        import openai

        # Build the base URL: project_endpoint + /openai/v1
        base_url = self.project_endpoint.rstrip("/") + "/openai/v1"

        client = openai.OpenAI(
            api_key=self.api_key,
            base_url=base_url,
        )

        logger.info("=" * 80)
        logger.info("🚀 Step 1: Send text to the Azure Agent.")
        logger.info("-" * 40)
        logger.info(f"📄 INPUT TEXT SNIPPET:\n{ai_jd[:500]}...")
        logger.info("=" * 80)

        response = client.responses.create(
            input=ai_jd,
            extra_body={
                "agent_reference": {
                    "name": self.agent_name,
                    "type": "agent_reference",
                }
            },
        )

        raw_text = response.output_text or ""
        return self._parse_agent_response(raw_text)

    # ─────────────────────────────────────────────────────────────────────────
    # Response parsing
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_agent_response(self, raw_text: str) -> Dict:
        """
        Step 2: Receiving response.
        Step 3: Extracting taxonomy mappings. 
        """
        logger.info("=" * 80)
        logger.info("🚀 Step 2: Receive the complete response payload from the Azure Agent.")
        logger.info("-" * 40)

        # Parse and log the raw response nicely
        try:
            temp_parsed = json.loads(raw_text)
            pretty_json = json.dumps(temp_parsed, indent=2)
            logger.info(f"📦 FULL AGENT RESPONSE:\n{pretty_json}")
        except:
             logger.info(f"📁 RAW AGENT RESPONSE:\n{raw_text}")

        logger.info("-" * 40)

        # Strip markdown code fences if present
        text = raw_text.strip()
        match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if match:
            text = match.group(1).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Try extracting first {...} block
            brace_match = re.search(r"\{[\s\S]+\}", text)
            if brace_match:
                parsed = json.loads(brace_match.group(0))
            else:
                logger.error("❌ AzureAgentService: Could not parse JSON from agent response.")
                raise ValueError(f"Could not parse JSON from agent response")

        roles  = parsed.get("job_roles",  [])
        # Handle both "job_skills" and "skills" as keys
        skills = parsed.get("job_skills") or parsed.get("skills") or []
        
        logger.info("🎯 Step 3: Perform taxonomy mapping for exact Role and Skill values.")
        logger.info("-" * 40)
        
        # Log Role Mapping
        for r in roles:
            raw_val = r.get("extracted_title") or r.get("raw_label") or "Unknown"
            k17000 = r.get("ROLE_K17000", "None")
            logger.info(f"   👔 Role Mapping  : '{raw_val}' ────▶ '{k17000}'")
            
        # Log Skill Mapping
        for s in skills:
            raw_val = s.get("extracted_skill") or s.get("raw_label") or "Unknown"
            mapped = s.get("skill_mapped", "None")
            logger.info(f"   🛠️  Skill Mapping : '{raw_val}' ────▶ '{mapped}'")

        logger.info("=" * 80)

        # Ensure the downstream code sees a consistent key
        parsed["job_roles"] = roles
        parsed["job_skills"] = skills
        return parsed

    # ─────────────────────────────────────────────────────────────────────────
    # Conversion helpers (agent JSON → rubric dicts)
    # ─────────────────────────────────────────────────────────────────────────

    def convert_to_rubric_roles(self, job_roles: List[Dict], target_job_title: str = "") -> List[Dict]:
        """
        Extracts ROLE_K17000 as primary and K10000-K10 hierarchy as similar_titles.
        Groups multiple JD-extracted titles that map to the same canonical taxonomy role.
        Excludes the raw JD-extracted title from the similar list per user requirement.
        """
        logger.info(f"🔍 DEBUG: Raw Grounded Roles from Agent: {job_roles}")
        
        # Grouping by canonical role (K17000)
        grouping = {} # canonical_name_upper -> { canonical_val, similar_titles_set }

        for item in job_roles:
            # Safely get canonical role, handling missing fields
            try:
                canonical = item.get("ROLE_K17000") or next((item.get(c) for c in ROLE_COLUMNS if item.get(c)), None)
            except Exception as e:
                logger.warning(f"⚠️ Error processing role item {item}: {e}")
                continue
            
            if not canonical or str(canonical).upper() in ["GUARDRAIL", "GUARDRAILS", "NULL", "NONE", "EMPTY"]:
                continue
                
            key = str(canonical).upper()
            if key not in grouping:
                grouping[key] = {
                    "value": canonical,
                    "similar_titles": []
                }
            
            # 1. REMOVED: Do NOT add the specific title extracted from the JD to the similar list
            # We only want verified taxonomy hierarchy to show up
            
            # 2. Add the hierarchy levels in Specific to Broad order (K10000 down to K10)
            for col in ROLE_COLUMNS:
                if col == "ROLE_K17000": continue # skip primary
                try:
                    val = item.get(col)
                    if val and str(val).upper() not in ["GUARDRAIL", "GUARDRAILS", "NULL", "NONE", "EMPTY"]:
                        val_str = str(val)
                        if val_str != canonical and val_str not in grouping[key]["similar_titles"]:
                            grouping[key]["similar_titles"].append(val_str)
                except Exception as e:
                    logger.warning(f"⚠️ Error processing role column {col}: {e}")
                    continue

        # Convert grouping back to list
        result = []
        for key, data in grouping.items():
            result.append({
                "value":          data.get("value", ""),
                "minYears":       0,
                "recent":         False,
                "matchType":      "Similar", 
                "required":       "Required",
                "source":         "PAIR",
                "similar_titles": data.get("similar_titles", []) or []
            })

        return result

    def convert_to_rubric_skills(self, job_skills: List[Dict]) -> List[Dict]:
        """
        Extracts skill_mapped as primary and skill_k15000-k15 hierarchy as similar_skills.
        Groups multiple JD-extracted skills that map to the same canonical taxonomy skill.
        Excludes the raw JD-extracted skill from the similar list per user requirement.
        """
        # Grouping by canonical skill (skill_mapped)
        grouping = {} # canonical_name_upper -> { canonical_val, similar_skills_set }
        
        for item in job_skills:
            try:
                canonical = item.get("skill_mapped")
                
                if not canonical or str(canonical).upper() in ["GUARDRAIL", "GUARDRAILS", "NULL", "NONE", "EMPTY", "BOARD CERTIFIED", "NONEVALUE"]:
                    continue
                
                key = str(canonical).upper()
                if key not in grouping:
                    grouping[key] = {
                        "value": canonical,
                        "similar_skills": [],
                        "required": item.get("required", "Required")
                    }

                # 1. REMOVED: Do NOT add the specific skill extracted from the JD to the similar list
                
                # 2. Add the hierarchy levels in Specific to Broad order (K15000 down to K15)
                for col in SKILL_COLUMNS:
                    if col == "skill_mapped": continue # skip primary
                    try:
                        val = item.get(col)
                        if val and str(val).upper() not in ["GUARDRAIL", "GUARDRAILS", "NULL", "NONE", "EMPTY", "NONEVALUE"]:
                            val_str = str(val)
                            if val_str != canonical and val_str not in grouping[key]["similar_skills"]:
                                grouping[key]["similar_skills"].append(val_str)
                    except Exception as e:
                        logger.warning(f"⚠️ Error processing skill column {col}: {e}")
                        continue
            except Exception as e:
                logger.warning(f"⚠️ Error processing skill item {item}: {e}")
                continue

        # Convert grouping back to list
        result = []
        for key, data in grouping.items():
            result.append({
                "value":          data["value"],
                "minYears":       0,
                "recent":         False,
                "matchType":      "Similar",
                "required":       data["required"],
                "category":       "grounded", # Placeholder to be categorized by LLM
                "source":         "PAIR",
                "similar_skills": list(data["similar_skills"])
            })

        return result

    def convert_to_profile_skills(self, agent_skills: List[Dict]) -> List[Dict]:
        """
        Maps Azure Agent response skills to standard CandidateProfile SkillProfileEntry.
        """
        from core.models import SkillProfileEntry
        
        grounded = []
        seen = set()
        
        for item in agent_skills:
            # We prioritize 'skill_mapped' as the canonical taxonomy name
            canonical = item.get("skill_mapped") or item.get("skill_k15000") or item.get("extracted_skill")
            
            if not canonical or str(canonical).upper() in ["GUARDRAIL", "GUARDRAILS", "NULL", "NONE", "EMPTY", "NONEVALUE"]:
                continue
                
            key = str(canonical).lower().strip()
            if key not in seen:
                seen.add(key)
                grounded.append({
                    "skill_slug": canonical,
                    "total_months": 0, # To be filled by matching logic or LLM later if needed
                    "last_used": "recent",
                    "competency_level": "grounded",
                    "sources": ["Azure AI Agent Grounding"]
                })
        
        return grounded
