"""One-shot probe of JobDiva /apiv2/jobdiva/JobAgentSearch.

Auths exactly like services.jobdiva (GET /api/authenticate with
clientid/username/password) and then issues the same JobAgentSearch
GET the BE makes, printing status, timing, headers, and body so we
can see what JobDiva is actually returning when the BE logs an
empty 'JobAgentSearch error: '.

Usage:
    ./venv/bin/python probe_jobagent.py [numeric_job_id] [resume_count]
"""
import os
import sys
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("JOBDIVA_API_URL")
CLIENT_ID = os.getenv("JOBDIVA_CLIENT_ID")
USERNAME = os.getenv("JOBDIVA_USERNAME")
PASSWORD = os.getenv("JOBDIVA_PASSWORD")

JOB_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 31783195  # 26-02576
RESUME_COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 400

print(f"[probe] API_URL set: {bool(API_URL)}  CLIENT_ID set: {bool(CLIENT_ID)}  "
      f"USERNAME set: {bool(USERNAME)}  PASSWORD set: {bool(PASSWORD)}")
print(f"[probe] JOB_ID={JOB_ID}  RESUME_COUNT={RESUME_COUNT}")

def auth() -> str:
    with httpx.Client(timeout=30.0) as c:
        t0 = time.time()
        r = c.get(f"{API_URL}/api/authenticate",
                  params={"clientid": CLIENT_ID, "username": USERNAME, "password": PASSWORD})
        dt = time.time() - t0
        print(f"[auth] status={r.status_code}  elapsed={dt:.2f}s  len={len(r.text)}")
        if r.status_code != 200:
            print(f"[auth] body: {r.text[:500]}")
            sys.exit(1)
        token = r.text.replace('"', '').strip()
        print(f"[auth] token_prefix={token[:12]}...  token_len={len(token)}")
        return token

def probe(token: str):
    url = f"{API_URL}/apiv2/jobdiva/JobAgentSearch"
    params = {"jobId": JOB_ID, "resumeCount": RESUME_COUNT}
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[probe] GET {url}  params={params}")
    t0 = time.time()
    try:
        with httpx.Client(timeout=130.0) as c:
            r = c.get(url, params=params, headers=headers)
        dt = time.time() - t0
        print(f"[probe] status={r.status_code}  elapsed={dt:.2f}s  body_len={len(r.text)}")
        ct = r.headers.get("content-type", "")
        print(f"[probe] content-type={ct}")
        body = r.text or ""
        print(f"[probe] body[:1200]={body[:1200]}")
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, dict):
                    rows = data.get("data") or []
                    print(f"[probe] json.keys={list(data.keys())[:10]}  rows={len(rows)}")
                elif isinstance(data, list):
                    print(f"[probe] json is list, len={len(data)}")
            except Exception as je:
                print(f"[probe] json parse error: {je!r}")
    except Exception as e:
        dt = time.time() - t0
        print(f"[probe] EXCEPTION after {dt:.2f}s  type={type(e).__name__}  repr={e!r}  str='{e}'")

if __name__ == "__main__":
    token = auth()
    probe(token)
