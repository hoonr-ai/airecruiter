import sqlalchemy
from sqlalchemy import text
from core.config import DATABASE_URL

try:
    engine = sqlalchemy.create_engine(DATABASE_URL)
    conn = engine.connect()
    
    # Find cities matching "Atlanta"
    result = conn.execute(text('''
        SELECT DISTINCT cfc.city 
        FROM "CandidateFieldCity" cfc
        WHERE cfc.city ILIKE '%Atlanta%'
        LIMIT 20
    '''))
    atlanta_cities = [r[0] for r in result]
    print(f"✅ Cities matching 'Atlanta': {atlanta_cities}")
    
    # Count candidates in Atlanta-like cities with SQL skill
    result = conn.execute(text('''
        SELECT COUNT(DISTINCT c.id)
        FROM "Candidate" c
        JOIN "CandidateFieldCity" cfc ON c.city = cfc.id
        JOIN "CandidatesSkills" cs ON cs."candidateId" = c.id
        JOIN "CandidatesSkillFieldSkillFinal" sf ON cs."skillFinal" = sf.id
        WHERE c."optedOut" IS NOT TRUE
        AND cfc.city ILIKE '%Atlanta%'
        AND sf."skillFinal" ILIKE '%SQL%'
    '''))
    count = result.fetchone()[0]
    print(f"✅ Candidates in Atlanta with SQL: {count}")
    
    # Test without location filter
    result = conn.execute(text('''
        SELECT COUNT(DISTINCT c.id)
        FROM "Candidate" c
        JOIN "CandidatesSkills" cs ON cs."candidateId" = c.id
        JOIN "CandidatesSkillFieldSkillFinal" sf ON cs."skillFinal" = sf.id
        WHERE c."optedOut" IS NOT TRUE
        AND sf."skillFinal" ILIKE '%SQL%'
    '''))
    count_no_loc = result.fetchone()[0]
    print(f"✅ Candidates with SQL (no location): {count_no_loc}")
    
    conn.close()
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
