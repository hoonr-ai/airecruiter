import json
import os
import psycopg2
from psycopg2.extras import RealDictCursor

# Connect to the prod or local DB if possible.
# Since local DB is down via socket, maybe it works via TCP?
try:
    conn = psycopg2.connect("dbname=airecruiter user=postgres host=127.0.0.1")
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT sc.name, la.payload FROM sourced_candidates sc JOIN latest_audit la ON sc.candidate_id = la.candidate_id WHERE sc.name ILIKE '%Jayaprabakar%'")
    rows = cur.fetchall()
    for r in rows:
        print('NAME:', r['name'])
        payload = json.loads(r['payload']) if isinstance(r['payload'], str) else r['payload']
        print('PAYLOAD:', json.dumps(payload, indent=2))
except Exception as e:
    print("Error:", e)
