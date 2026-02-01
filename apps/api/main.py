from fastapi import FastAPI
from typing import List
from models import JobDescription, MatchResult
from matcher import mock_match_candidates

app = FastAPI(title="Hoonr.ai API")

@app.get("/health")
def read_health():
    return {"status": "ok", "service": "hoonr-api"}

@app.post("/match", response_model=List[MatchResult])
def match_candidates(jd: JobDescription):
    """
    Accepts a Job Description and returns ranked candidates.
    """
    results = mock_match_candidates(jd)
    return results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
