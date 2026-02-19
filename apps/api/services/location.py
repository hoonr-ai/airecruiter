import os
import json
from openai import AsyncOpenAI
from pydantic import BaseModel

class LocationVerdict(BaseModel):
    is_within_range: bool
    distance_estimate: str # e.g. "15 miles", "Different Country"
    reason: str

class LocationService:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None
        
    async def check_proximity(self, candidate_loc: str, job_loc: str, work_mode: str) -> LocationVerdict:
        """
        Semantically checks if candidate is within commuting distance.
        """
        if not candidate_loc or not job_loc or not self.client:
            return LocationVerdict(is_within_range=True, distance_estimate="Unknown", reason="Location data unavailable (check resume or API key).")

        # Logic: If Remote, always true
        if work_mode.lower() == "remote":
            return LocationVerdict(is_within_range=True, distance_estimate="N/A", reason="Role is Remote.")

        prompt = f"""
        Determine if the Candidate Location is within commuting distance (approx 50 miles / 80 km) of the Job Location.
        
        Candidate Location: {candidate_loc}
        Job Location: {job_loc}
        
        Output JSON:
        {{
            "is_within_range": boolean,
            "distance_estimate": "string (e.g. 'Same City', '400 miles', 'Different Country')",
            "reason": "Short explanation (e.g. 'Newark is a suburb of NYC', 'London is in UK, job in US')"
        }}
        """

        try:
            completion = await self.client.beta.chat.completions.parse(
                model="gpt-4o-mini", # Cheap model is fine for geography
                messages=[
                    {"role": "system", "content": "You are a Geography Distance Calculator. Be realistic about commuting."},
                    {"role": "user", "content": prompt}
                ],
                response_format=LocationVerdict,
                temperature=0.0
            )
            return completion.choices[0].message.parsed
        except Exception as e:
            print(f"⚠️ Location Check Error: {e}")
            return LocationVerdict(is_within_range=True, distance_estimate="Error", reason="Location check failed.")
