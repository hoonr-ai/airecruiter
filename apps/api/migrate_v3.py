import sqlalchemy
from sqlalchemy import text
from core.config import DATABASE_URL

def migrate_v3():
    if not DATABASE_URL:
        print("❌ DATABASE_URL not found.")
        return

    print("🚀 Running Database Schema Update (v3)...")
    engine = sqlalchemy.create_engine(DATABASE_URL)
    
    try:
        with engine.connect() as conn:
            print("Adding description and location columns...")
            
            # Add missing columns for UI parity
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS description TEXT;"))
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS city TEXT;"))
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS state TEXT;"))
            conn.execute(text("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS zip TEXT;"))
            
            conn.commit()
            print("✅ Columns added successfully.")
            
    except Exception as e:
        print(f"❌ Schema update failed: {e}")

if __name__ == "__main__":
    migrate_v3()
