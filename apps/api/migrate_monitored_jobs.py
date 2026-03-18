import os
import json
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_JSON_PATH = os.path.join(SCRIPT_DIR, "monitored_jobs.json")

def setup_and_migrate():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found in environment.")
        return

    print("🚀 Starting Database Setup and Migration...")
    engine = sqlalchemy.create_engine(DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            # 1. Create Table
            print("Creating table 'monitored_jobs' if not exists...")
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitored_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT,
                    customer TEXT,
                    title TEXT,
                    recruiter_email TEXT,
                    work_authorization TEXT,
                    ai_description TEXT,
                    job_notes TEXT,
                    added_at TEXT,
                    last_updated TEXT
                );
            """))
            conn.commit()
            
            # 2. Load JSON Data
            if not os.path.exists(JOBS_JSON_PATH):
                print(f"⚠️ {JOBS_JSON_PATH} not found. Nothing to migrate.")
                return

            with open(JOBS_JSON_PATH, 'r') as f:
                data = json.load(f)
            
            jobs = data.get("jobs", {})
            if not jobs:
                print("No jobs found in JSON.")
                return

            print(f"Found {len(jobs)} jobs in JSON. Migrating...")
            
            for job_id, details in jobs.items():
                print(f"  - Migrating {job_id}: {details.get('title')}")
                conn.execute(text("""
                    INSERT INTO monitored_jobs (
                        job_id, status, customer, title, recruiter_email, work_authorization, ai_description, job_notes, added_at, last_updated
                    ) VALUES (:job_id, :status, :customer, :title, :recruiter_email, :work_authorization, :ai_description, :job_notes, :added_at, :last_updated)
                    ON CONFLICT (job_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        customer = EXCLUDED.customer,
                        title = EXCLUDED.title,
                        recruiter_email = EXCLUDED.recruiter_email,
                        work_authorization = EXCLUDED.work_authorization,
                        ai_description = EXCLUDED.ai_description,
                        job_notes = EXCLUDED.job_notes,
                        last_updated = EXCLUDED.last_updated;
                """), {
                    "job_id": job_id,
                    "status": details.get("status"),
                    "customer": details.get("customer"),
                    "title": details.get("title"),
                    "recruiter_email": details.get("recruiter_email") or details.get("recruiter_emails"),
                    "work_authorization": details.get("work_authorization"),
                    "ai_description": details.get("ai_description"),
                    "job_notes": details.get("job_notes"),
                    "added_at": details.get("added_at"),
                    "last_updated": details.get("last_updated")
                })
            
            conn.commit()
            print(f"✅ Migration Complete. {len(jobs)} jobs processed.")

    except Exception as e:
        print(f"❌ Migration Failed: {e}")

if __name__ == "__main__":
    setup_and_migrate()
