import os
import asyncio
from services.vetted import vetted_service

# Manual .env loading
def load_env_manual(filepath):
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

load_env_manual("apps/api/.env")

async def test_service_search():
    print("🚀 Testing VettedService.search_candidates for 'Java'...")
    
    # 1. Initialize engine
    vetted_service.__init__()
    if not vetted_service.db_url:
        print("❌ DATABASE_URL is missing!")
        return

    # 1.5 RAW SQL CHECK
    try:
        conn = vetted_service.engine.connect()
        from sqlalchemy import text
        result = conn.execute(text("""
            SELECT COUNT(DISTINCT c.id)
            FROM "Candidate" c
            JOIN "CandidatesSkills" cs ON cs."candidateId" = c.id
            JOIN "CandidatesSkillFieldSkillFinal" sf ON cs."skillFinal" = sf.id
            WHERE c."optedOut" IS NOT TRUE
            AND sf."skillFinal" ILIKE '%Java%'
        """))
        count = result.fetchone()[0]
        print(f"📊 RAW DB COUNT for 'Java': {count}")
        conn.close()
    except Exception as e:
        print(f"❌ Raw SQL Check Failed: {e}")

    # 2. Search via Service
    skills = ["Java"]
    location = None
    results = await vetted_service.search_candidates(skills, location)
    
    print(f"✅ Found {len(results)} candidates via Service Logic")
    
    if len(results) > 0:
        first = results[0]
        print(f"Sample Candidate: {first['firstName']} (Source: {first['source']})")
        print(f"Skills: {first['skills']}")
    else:
        print("❌ No results found via Service Logic!")

if __name__ == "__main__":
    asyncio.run(test_service_search())
