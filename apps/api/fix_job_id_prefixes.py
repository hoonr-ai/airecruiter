import os
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

def migrate():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found in environment.")
        return

    print("🚀 Starting Database Migration (v2: Column Reordering & New Fields)...")
    engine = sqlalchemy.create_engine(DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            # 1. Create New Table with proper order
            print("Creating 'monitored_jobs_new' with proper column order...")
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitored_jobs_new (
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
            
            # 2. Check if old table exists
            res = conn.execute(text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'monitored_jobs')"))
            exists = res.fetchone()[0]
            
            if exists:
                print("Copying data from old 'monitored_jobs' to 'monitored_jobs_new'...")
                # We need to be careful with column names in case they changed
                # The old columns were: job_id, status, customer, title, added_at, last_updated, ai_description, job_notes
                conn.execute(text("""
                    INSERT INTO monitored_jobs_new (job_id, status, customer, title, ai_description, job_notes, added_at, last_updated)
                    SELECT job_id, status, customer, title, ai_description, job_notes, added_at, last_updated
                    FROM monitored_jobs
                    ON CONFLICT (job_id) DO NOTHING;
                """))
                
                print("Dropping old 'monitored_jobs' table...")
                conn.execute(text("DROP TABLE monitored_jobs CASCADE"))
            
            print("Renaming 'monitored_jobs_new' to 'monitored_jobs'...")
            conn.execute(text("ALTER TABLE monitored_jobs_new RENAME TO monitored_jobs"))
            
            conn.commit()
            print("✅ Migration Complete!")

    except Exception as e:
        print(f"❌ Migration Failed: {e}")

if __name__ == "__main__":
    migrate()
