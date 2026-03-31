import psycopg2
import os
import sys
from pathlib import Path

# Add the apps/api directory to sys.path to import core
api_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(api_dir))

from core.config import DATABASE_URL

def migrate():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 1. Add bot_introduction to monitored_jobs
        print("Adding bot_introduction to monitored_jobs...")
        cur.execute("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS bot_introduction TEXT;")

        # 2. Create job_screen_questions table
        print("Creating job_screen_questions table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_screen_questions (
                id SERIAL PRIMARY KEY,
                jobdiva_id VARCHAR(50) NOT NULL,
                question_text TEXT NOT NULL,
                pass_criteria TEXT,
                is_default BOOLEAN DEFAULT FALSE,
                category VARCHAR(50) DEFAULT 'other',
                order_index INTEGER DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jsq_jobdiva_id ON job_screen_questions(jobdiva_id);")

        conn.commit()
        print("✅ Migration successful")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Migration failed: {e}")

if __name__ == "__main__":
    migrate()
