import os
import psycopg2
from dotenv import load_dotenv

import sys

def setup_persistent_summary():
    load_dotenv("apps/api/.env")
    
    # Check for --local flag
    use_local = "--local" in sys.argv
    db_var = "DATABASE_URL" if use_local else "SUPABASE_DB_URL"
    
    db_url = os.getenv(db_var)
    if not db_url:
        print(f"❌ Error: {db_var} not found.")
        return

    print(f"🔌 Connecting to: {db_var}")

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()

        # 1. Create the persistent summary table
        print("🛠️ Creating Table: api_usage_summary...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_usage_summary (
                service VARCHAR(100),
                model VARCHAR(50),
                num_requests BIGINT DEFAULT 0,
                total_prompt_tokens BIGINT DEFAULT 0,
                total_completion_tokens BIGINT DEFAULT 0,
                total_tokens BIGINT DEFAULT 0,
                total_cost DECIMAL(15, 6) DEFAULT 0.0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (service, model)
            );
        """)

        # 2. Function to update the summary
        print("🛠️ Creating Trigger Function: fn_update_usage_summary...")
        cur.execute("""
            CREATE OR REPLACE FUNCTION fn_update_usage_summary()
            RETURNS TRIGGER AS $$
            BEGIN
                INSERT INTO api_usage_summary (service, model, num_requests, total_prompt_tokens, total_completion_tokens, total_tokens, total_cost, last_updated)
                VALUES (NEW.service, NEW.model, 1, NEW.prompt_tokens, NEW.completion_tokens, NEW.total_tokens, NEW.cost_usd, CURRENT_TIMESTAMP)
                ON CONFLICT (service, model) DO UPDATE SET
                    num_requests = api_usage_summary.num_requests + 1,
                    total_prompt_tokens = api_usage_summary.total_prompt_tokens + NEW.prompt_tokens,
                    total_completion_tokens = api_usage_summary.total_completion_tokens + NEW.completion_tokens,
                    total_tokens = api_usage_summary.total_tokens + NEW.total_tokens,
                    total_cost = api_usage_summary.total_cost + NEW.cost_usd,
                    last_updated = CURRENT_TIMESTAMP;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)

        # 3. Create the trigger
        print("🛠️ Creating Trigger: trg_on_usage_log_insert...")
        cur.execute("""
            DROP TRIGGER IF EXISTS trg_on_usage_log_insert ON api_usage_logs;
            CREATE TRIGGER trg_on_usage_log_insert
            AFTER INSERT ON api_usage_logs
            FOR EACH ROW
            EXECUTE FUNCTION fn_update_usage_summary();
        """)

        # 4. Optional: Populate the summary table from existing logs
        print("🛠️ Syncing existing logs to summary table...")
        cur.execute("""
            TRUNCATE TABLE api_usage_summary;
            INSERT INTO api_usage_summary (service, model, num_requests, total_prompt_tokens, total_completion_tokens, total_tokens, total_cost, last_updated)
            SELECT 
                service, 
                model, 
                COUNT(*) as num_requests, 
                SUM(prompt_tokens) as total_prompt_tokens, 
                SUM(completion_tokens) as total_completion_tokens, 
                SUM(total_tokens) as total_tokens, 
                SUM(cost_usd) as total_cost,
                MAX(timestamp) as last_updated
            FROM api_usage_logs
            GROUP BY service, model;
        """)

        conn.commit()
        print("✅ Success: Persistent usage summary table and trigger are active.")
        cur.close()
        conn.close()

    except Exception as e:
        print(f"❌ Error setting up persistent summary: {e}")

if __name__ == "__main__":
    setup_persistent_summary()
