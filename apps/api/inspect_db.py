
import os
import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes
import pg8000

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL")

print(f"🔍 Inspecting Database URL: {DATABASE_URL}")

if not DATABASE_URL:
    print("❌ DATABASE_URL is missing via env!")
    exit(1)

# Connect
def getconn():
    # SQLAlchemy requires 'postgresql://', but some legacy envs use 'postgres://'
    url = DATABASE_URL.replace("postgres://", "postgresql://")
    return sqlalchemy.create_engine(url)

try:
    engine = getconn()
    conn = engine.connect()
    # conn.autocommit = True # SQLAlchemy handles text execution
    
    # List Tables
    result = conn.execute(sqlalchemy.text("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
    """))
    tables = result.fetchall()
    
    print(f"\nFound {len(tables)} tables:")
    for t in tables:
        print(f" - {t[0]}")
        
        if t[0] in ["CandidatesSkillFieldSkillFinal"]: 
             print(f"--- TABLE: {t[0]} ---")
             col_result = conn.execute(sqlalchemy.text(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{t[0]}' AND table_schema = 'public'"))
             cols = col_result.fetchall()
             for c in cols:
                 print(f"    - {c[0]}: {c[1]}")
             print("-----------------------")
            
except Exception as e:
    print(f"❌ Error: {e}")
finally:
    if 'cur' in locals(): cur.close()
    if 'conn' in locals(): conn.close()
