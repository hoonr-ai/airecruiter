
import os
import psycopg2
from google.cloud.sql.connector import Connector, IPTypes
import pg8000
import sqlalchemy

# Configuration
INSTANCE_CONNECTION_NAME = os.getenv("CLOUDSQL_CONNECTION_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASSWORD")
# Try these DBs in order
TARGET_DBS = ["postgres", "skills-db-v1", "skills_db", "test", "cloudsqladmin"]

print(f"🔍 Testing Cloud SQL Connection...")
print(f"   Instance: {INSTANCE_CONNECTION_NAME}")
print(f"   User:     {DB_USER}")

if not INSTANCE_CONNECTION_NAME:
    print("❌ CLOUDSQL_CONNECTION_NAME is missing in env!")
    exit(1)

connector = Connector()

def getconn(db_name):
    print(f"   -> Connecting to DB: '{db_name}'...")
    try:
        conn = connector.connect(
            INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=DB_USER,
            password=DB_PASS,
            db=db_name,
            ip_type=IPTypes.PUBLIC
        )
        return conn
    except Exception as e:
        print(f"      ❌ Connect Failed: {e}")
        return None

def test_db(db_name):
    conn = getconn(db_name)
    if not conn:
        return False

    try:
        # We have a raw pg8000 connection
        # Use simple query to list tables
        cursor = conn.cursor()
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public';")
        tables = [row[0] for row in cursor.fetchall()]
        
        print(f"      ✅ Connected! Tables: {tables}")
        
        if "skill_nodes" in tables:
            print(f"      🎉 FOUND IT! The correct DB_NAME is: '{db_name}'")
            return True
            
        cursor.close()
        conn.close()
    except Exception as e:
         print(f"      ⚠️ Query Failed: {e}")
         
    return False

# Main Loop
found = False
for db in TARGET_DBS:
    print(f"\n--- Testing '{db}' ---")
    if test_db(db):
        found = True
        break

if not found:
    print("\n❌ Could not find 'skill_nodes' in any of the tested databases.")
else:
    print("\n✅ Test Complete.")
