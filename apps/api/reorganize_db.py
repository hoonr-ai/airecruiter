import os
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

env_path = "/Users/swatipandey/Desktop/airecruiter/apps/api/.env"
load_dotenv(env_path)

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

def reorganize_table():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found.")
        return

    print("🚀 Reorganizing 'monitored_jobs' table schema...")
    engine = sqlalchemy.create_engine(DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            # 1. Drop existing table
            print("Dropping existing table...")
            conn.execute(text("DROP TABLE IF EXISTS monitored_jobs;"))
            
            # 2. Create table with appropriate column order and renamed original JD field
            print("Creating table with new column order...")
            conn.execute(text("""
                CREATE TABLE monitored_jobs (
                    job_id TEXT PRIMARY KEY,
                    title TEXT,
                    customer TEXT,
                    status TEXT,
                    jobdiva_description TEXT, -- Renamed for clarity
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    employment_type TEXT,
                    start_date TEXT,
                    pay_rate TEXT,
                    openings TEXT,
                    posted_date TEXT,
                    work_authorization TEXT,
                    recruiter_email TEXT,
                    ai_description TEXT,
                    job_notes TEXT,
                    added_at TEXT,
                    last_updated TEXT
                );
            """))
            conn.commit()
            print("✅ 'monitored_jobs' table reorganized successfully.")
            
    except Exception as e:
        print(f"❌ Reorganization failed: {e}")

if __name__ == "__main__":
    reorganize_table()
