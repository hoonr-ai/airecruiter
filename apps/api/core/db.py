import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes
import pg8000
import psycopg2
import psycopg2.extras
import json
from .config import (
    INSTANCE_CONNECTION_NAME, DB_USER, DB_PASSWORD, DB_NAME, DATABASE_URL
)
# Note: DB_PASS is mapped to DB_PASSWORD from config
DB_PASS = DB_PASSWORD


def get_db_connection():
    """Canonical psycopg2 connection to the application database.

    connect_timeout=5 → slow or unreachable Postgres must fail fast. Without a
    bound, a contested DB (locks, pool saturation, network blip) can hang
    uvicorn workers for the full TCP default (~2 min), which is what the v21
    QA slowness report on `/jobs/monitored` tracked back to.
    """
    if not DATABASE_URL:
        raise Exception("DATABASE_URL not configured")
    return psycopg2.connect(DATABASE_URL, connect_timeout=5)


def get_dict_cursor_connection():
    """psycopg2 connection whose default cursor returns dicts (RealDictCursor)."""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL not configured")
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=5,
    )

# Global Pool
pool = None

def getconn():
    with Connector() as connector:
        conn = connector.connect(
            INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=DB_USER,
            password=DB_PASS,
            db=DB_NAME,
            ip_type=IPTypes.PUBLIC
        )
        return conn

def init_connection_pool():
    # Helper if we wanted SQLAlchemy pool, but for simple Cloud Run direct strictness,
    # let's just use raw connection for now to avoid complexity, or a simple creator.
    # Ideally we'd use SQLAlchemy create_engine with creator=getconn
    pass

# Simple functionality for saving jobs
def save_job(parsed_data: dict, description: str = "") -> str:
    """
    Saves the parsed job JSON (TOON/Schema) to the database.
    Returns the Job ID (UUID).
    """
    if not INSTANCE_CONNECTION_NAME:
        print("⚠️ No DB Connection Name. Skipping persistence.")
        return "mock-job-id"
        
    try:
        # Use connector directly for single transaction
        connector = Connector()
        conn = connector.connect(
            INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=DB_USER,
            password=DB_PASS,
            db=DB_NAME,
            ip_type=IPTypes.PUBLIC
        )
        conn.autocommit = True
        cursor = conn.cursor()
        
        title = parsed_data.get("title", "Untitled Job")
        
        # Insert
        insert_query = """
            INSERT INTO jobs (title, description, parsed_data)
            VALUES (%s, %s, %s)
            RETURNING id;
        """
        
        cursor.execute(insert_query, (title, description, json.dumps(parsed_data)))
        job_id = cursor.fetchone()[0]
        
        cursor.close()
        conn.close()
        connector.close()
        
        return str(job_id)
        
    except Exception as e:
        print(f"❌ DB Error saving job: {e}")
        return "error-saving-job"
