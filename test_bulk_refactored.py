import json
import uuid
import requests
import argparse
import csv
import os
from pathlib import Path
from datetime import datetime

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


def load_candidates_from_csv(csv_path: str) -> list:
    """
    Load candidates from CSV file.
    Expected columns: name, email, phone, location, title, skills, summary
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    
    candidates = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            candidates.append({
                "name": row.get("name", "").strip(),
                "email": row.get("email", "").strip(),
                "phone": row.get("phone", "").strip(),
                "location": row.get("location", "").strip(),
                "title": row.get("title", "").strip(),
                "skills": row.get("skills", "").strip(),
                "summary": row.get("summary", "").strip(),
            })
    return candidates


def load_candidates_from_excel(excel_path: str, sheet_name=0) -> list:
    """
    Load candidates from Excel file.
    Expected columns: name, email, phone, location, title, skills, summary
    Requires pandas to be installed.
    """
    if not PANDAS_AVAILABLE:
        raise ImportError("pandas is required to read Excel files. Install with: pip install pandas openpyxl")
    
    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Excel file not found: {excel_path}")
    
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    
    candidates = []
    for _, row in df.iterrows():
        candidates.append({
            "name": str(row.get("name", "")).strip(),
            "email": str(row.get("email", "")).strip(),
            "phone": str(row.get("phone", "")).strip(),
            "location": str(row.get("location", "")).strip(),
            "title": str(row.get("title", "")).strip(),
            "skills": str(row.get("skills", "")).strip(),
            "summary": str(row.get("summary", "")).strip(),
        })
    return candidates


def build_resume(name, email, phone, location, title, skills, summary):
    """Build a formatted resume text from candidate data."""
    first, *rest = name.split()
    last = " ".join(rest) if rest else ""

    return f"""
RESUME

{name}
{email} | {phone} | {location}

PROFESSIONAL SUMMARY
{summary}

CORE COMPETENCIES
{skills}

PROFESSIONAL EXPERIENCE

{title}
{location}
Relevant industry experience and technical proficiency.

EDUCATION
B.S. Computer Science

CERTIFICATIONS
Relevant industry certifications held and maintained.
"""


def test_bulk_interview(api_url="http://localhost:8001", candidates_file=None):
    """
    Test bulk interview with candidates from CSV/Excel file.
    Sends interviews through the local API /api/engage/send-bulk-interview endpoint in batches.
    
    Args:
        api_url: Base URL of the API (e.g., http://localhost:8001)
        candidates_file: Path to CSV or Excel file with candidate data
    """
    print("\n" + "="*60)
    print("  BULK INTERVIEW LOAD TEST")
    print("="*60)

    # Load candidates from file
    if not candidates_file:
        raise ValueError("--candidates-file is required. Provide a CSV or Excel file path.")
    
    file_ext = Path(candidates_file).suffix.lower()
    
    if file_ext == '.csv':
        print(f"\n📁 Loading candidates from CSV: {candidates_file}")
        candidates_data = load_candidates_from_csv(candidates_file)
    elif file_ext in ['.xlsx', '.xls']:
        print(f"\n📁 Loading candidates from Excel: {candidates_file}")
        candidates_data = load_candidates_from_excel(candidates_file)
    else:
        raise ValueError(f"Unsupported file format: {file_ext}. Use .csv or .xlsx")
    
    if not candidates_data:
        raise ValueError(f"No candidates found in {candidates_file}")
    
    print(f"  ✅ Loaded {len(candidates_data)} candidates")

    # Load base payload from JSON
    base_payload_path = Path(__file__).parent / "samplepayload.json"
    with open(base_payload_path) as f:
        base_payload = json.load(f)

    jd = base_payload["jd"]
    resumes = []
    real_candidate_ids = []

    print(f"\n🔧 Building candidate profiles from file...")
    for i, cand_data in enumerate(candidates_data, 1):
        name = cand_data.get("name", f"Candidate {i}")
        email = cand_data.get("email", f"candidate{i}@example.com")
        phone = cand_data.get("phone", f"+1202555{i:04d}")
        location = cand_data.get("location", "Remote")
        title = cand_data.get("title", "Engineer")
        skills = cand_data.get("skills", "Various technical skills")
        summary = cand_data.get("summary", "Skilled professional")
        
        cid = f"candidate_{i:03d}_{uuid.uuid4().hex[:6]}"
        
        first, *rest = name.split()
        last = " ".join(rest) if rest else ""

        resumes.append({
            "source_candidate_id": cid,
            "name": name,
            "candidate_name": name,
            "full_name": name,
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": phone,
            "raw_resume_text": build_resume(name, email, phone, location, title, skills, summary),
            "experience": f"{title} — {location}. {summary}",
            "summary": summary,
            "skills": skills,
            "education": "B.S. Computer Science",
        })
        real_candidate_ids.append(cid)
        print(f"  ✅ [{i:>3}/{len(candidates_data)}] {name} — {title}")

    test_run_label = datetime.now().strftime("%-d %b %Y %-I:%M:%S %p IST")

    # Test job descriptor
    test_jd = {
        "job_id": "BULK_TEST_FROM_CSV",
        "jobdiva_id": "BULK-TEST-CSV",
        "context": {
            "title": f"[TEST] Bulk Interview Load Test — {len(resumes)} Candidates ({test_run_label})",
            "customer_name": "PAIR Internal QA",
            "city": "Remote",
            "state": "NA",
            "location_type": "Remote",
            "jobdiva_description": f"Bulk interview test run with {len(resumes)} candidates from {Path(candidates_file).name}",
            "ai_description": "Automated QA load test — not a real job posting.",
            "recruiter_notes": "Do not engage. Test run only.",
        },
        "rubric": {
            "titles": [{"value": "QA Load Test Role", "minYears": 0, "recent": False, "matchType": "Exact", "required": "Required", "source": "Test"}],
            "skills": [{"value": "Bulk Interview Testing", "minYears": 1, "recent": True, "matchType": "Exact", "required": "Required"}],
            "education": [{"degree": "Any", "field": "Any", "required": "Optional"}],
        },
        "pre_screen_questions": [
            {"question_text": "This is a test interview. Can you confirm you are a test candidate?", "pass_criteria": "Answers yes", "is_default": True, "category": "test"},
        ],
    }

    BATCH_SIZE = 5
    batches = []
    for i in range(0, len(resumes), BATCH_SIZE):
        batch_resumes = resumes[i:i + BATCH_SIZE]
        batch_cids = real_candidate_ids[i:i + BATCH_SIZE]
        batches.append((batch_resumes, batch_cids))

    print(f"\n📦 Grouped {len(resumes)} candidates into {len(batches)} batches of {BATCH_SIZE}.")

    endpoint = f"{api_url.rstrip('/')}/api/engage/send-bulk-interview"
    if "pairbotqa.hoonr.ai" in api_url or "pairqa.hoonr.ai" in api_url:
        endpoint = f"{api_url.rstrip('/')}/api/bulk-interviews"

    print(f"  Endpoint: {endpoint}\n")

    total_results = []
    total_skipped = []
    failed_batches = 0

    def send_batch(batch_idx, batch_resumes, batch_cids):
        payload_obj = {
            "resumes": batch_resumes,
            "jd": test_jd,
            "company_intro": "PAIR Internal QA — Bulk Load Test",
            "interview_duration": "20-25",
            "source": test_run_label,
        }
        
        request_body = {
            "payload": json.dumps(payload_obj),
            "real_candidate_ids": batch_cids,
            "is_initial_launch": False,
            "dry_run": False,
            "notify_recruiters": False,
            "app_base_url": "http://localhost:3000",
        }

        # If hitting the internal AI Recruiter API, use request_body.
        json_payload = payload_obj if "bulk-interviews" in endpoint else request_body

        try:
            response = requests.post(endpoint, json=json_payload, timeout=180)
            try:
                return batch_idx, response.status_code, response.json()
            except Exception:
                # Server returned non-JSON — log raw text for diagnosis
                raw = response.text[:500] if response.text else "<empty body>"
                return batch_idx, response.status_code, {"raw_error": raw}
        except Exception as e:
            return batch_idx, None, {"raw_error": str(e)}

    # Production Curate flow processes batches SEQUENTIALLY (one at a time),
    # using a for-loop with await — NOT Promise.all / concurrent.
    # Source: apps/web/app/jobs/new/page.tsx line 5996
    for batch_idx, (b_resumes, b_cids) in enumerate(batches):
        b_idx, status_code, data = send_batch(batch_idx, b_resumes, b_cids)
        if status_code == 200:
            success = data.get("success", False)
            if success:
                results = data.get("data", [])
                skipped = data.get("skipped_already_sent", [])
                total_results.extend(results)
                total_skipped.extend(skipped)
                print(f"  ✅ Batch {batch_idx+1}/{len(batches)} SUCCESS: {len(results)} created, {len(skipped)} skipped.")
            else:
                failed_batches += 1
                print(f"  ❌ Batch {batch_idx+1}/{len(batches)} FAILED: {data.get('message', 'Unknown error')}")
        else:
            failed_batches += 1
            raw_err = data.get('raw_error', data) if isinstance(data, dict) else data
            print(f"  ❌ Batch {batch_idx+1}/{len(batches)} HTTP {status_code}: {raw_err}")

    print(f"\n📋 Interview Results (first 10):")
    for r in total_results[:10]:
        print(f"   [{r.get('candidate_name','?')}] interview_id={r.get('interview_id','—')}  email={r.get('candidate_email','?')}")
    if len(total_results) > 10:
        print(f"   ... and {len(total_results)-10} more")

    # Summary table
    print(f"\n{'='*60}")
    print(f"  BULK INTERVIEW TEST SUMMARY")
    print(f"{'='*60}")
    print(f"  Candidates sent    : {len(resumes)}")
    print(f"  Interviews created : {len(total_results)}")
    print(f"  Skipped (already)  : {len(total_skipped)}")
    print(f"  Failed Batches     : {failed_batches}")
    print(f"  Status             : {'✅ PASSED' if failed_batches == 0 and len(total_results) > 0 else '❌ FAILED'}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test bulk interview with candidates from CSV/Excel file")
    parser.add_argument("--url", type=str, default="http://localhost:8001", help="Base URL of the local API")
    parser.add_argument("--candidates-file", type=str, required=True, help="Path to CSV or Excel file with candidate data")
    args = parser.parse_args()
    test_bulk_interview(api_url=args.url, candidates_file=args.candidates_file)
