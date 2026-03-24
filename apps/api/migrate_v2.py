import os
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

def migrate_v2():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found.")
        return

    print("🚀 Running Database Schema Update (v2)...")
    engine = sqlalchemy.create_engine(DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            print("Adding columns for detailed job info...")
            
            # Use safety checks for PostgreSQL
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS pay_rate TEXT;"))
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS openings TEXT;"))
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS posted_date TEXT;"))
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS start_date TEXT;"))
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS employment_type TEXT;"))
            
            conn.commit()
            print("✅ Columns added successfully.")
            
    except Exception as e:
        print(f"❌ Schema update failed: {e}")

if __name__ == "__main__":
    migrate_v2()
