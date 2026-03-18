import os
import sqlalchemy
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

def verify():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found in environment.")
        return

    print("🔍 Verifying 'monitored_jobs' schema...")
    engine = sqlalchemy.create_engine(DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            # Check column order
            res = conn.execute(text("""
                SELECT column_name, ordinal_position 
                FROM information_schema.columns 
                WHERE table_name = 'monitored_jobs' 
                ORDER BY ordinal_position;
            """))
            cols = res.fetchall()
            
            print("\nTable: monitored_jobs")
            for c in cols:
                print(f" {c[1]}. {c[0]}")
            
            # Expected order:
            # 1. job_id
            # 2. status
            # 3. customer
            # 4. title
            # 5. recruiter_email
            # 6. work_authorization
            # 7. ai_description
            # 8. job_notes
            # 9. added_at
            # 10. last_updated
            
            expected = ["job_id", "status", "customer", "title", "recruiter_email", "work_authorization", "ai_description", "job_notes", "added_at", "last_updated"]
            actual = [c[0] for c in cols]
            
            if actual == expected:
                print("\n✅ SCHEMA VERIFIED: Column order is correct!")
            else:
                print("\n❌ SCHEMA MISMATCH!")
                print(f"Expected: {expected}")
                print(f"Actual:   {actual}")

    except Exception as e:
        print(f"❌ Verification Failed: {e}")

if __name__ == "__main__":
    verify()
