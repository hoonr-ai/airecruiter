import os
import sqlalchemy
from sqlalchemy import text

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

try:
    engine = sqlalchemy.create_engine(DATABASE_URL)
    conn = engine.connect()
    
    # Check what names look like for candidates with SQL
    result = conn.execute(text('''
        SELECT c.id, c.name, c.email
        FROM "Candidate" c
        JOIN "CandidatesSkills" cs ON cs."candidateId" = c.id
        JOIN "CandidatesSkillFieldSkillFinal" sf ON cs."skillFinal" = sf.id
        WHERE c."optedOut" IS NOT TRUE
        AND sf."skillFinal" ILIKE '%Java%'
        LIMIT 5
    '''))
    
    print("Sample candidates with Java skill:")
    for row in result:
        print(f"  ID: {row.id}")
        print(f"  Name: {row.name}")
        print(f"  Email: {row.email}")
        print(f"  Name length: {len(row.name) if row.name else 0}")
        print("  ---")
    
    conn.close()
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
