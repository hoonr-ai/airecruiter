import psycopg2
import json

def run():
    conn = psycopg2.connect("dbname=airecruiter user=postgres")
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            sc.jobdiva_id,
            sc.candidate_id, 
            sc.name,
            sc.source,
            mj.title
        FROM sourced_candidates sc
        LEFT JOIN monitored_jobs mj ON sc.jobdiva_id = mj.jobdiva_id
        WHERE sc.name ILIKE '%Jayaprabhakar%';
    """)
    for row in cur.fetchall():
        print(row)
run()
