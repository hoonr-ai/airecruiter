import os
import psycopg2
from dotenv import load_dotenv

# Step 2: Database Migration - Create job_criteria Table
def update_db():
    load_dotenv()
    db_url = os.getenv("SUPABASE_DB_URL")
    if not db_url:
        print("❌ SUPABASE_DB_URL not found in .env")
        return

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()

        # Create job_criteria table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_criteria (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                job_id VARCHAR(50) NOT NULL,
                name TEXT NOT NULL,
                weight FLOAT DEFAULT 1.0,
                is_required BOOLEAN DEFAULT FALSE,
                is_ai_generated BOOLEAN DEFAULT TRUE,
                category VARCHAR(50) DEFAULT 'Hard Filter',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE INDEX IF NOT EXISTS idx_job_criteria_job_id ON job_criteria(job_id);
        """)
        
        conn.commit()
        print("✅ Success: job_criteria table created/verified.")
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error updating database: {e}")

if __name__ == "__main__":
    update_db()
