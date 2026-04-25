import sqlalchemy
from google.cloud.sql.connector import Connector, IPTypes
import pg8000
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
import json
import threading
from .config import (
    INSTANCE_CONNECTION_NAME, DB_USER, DB_PASSWORD, DB_NAME, DATABASE_URL
)
# Note: DB_PASS is mapped to DB_PASSWORD from config
DB_PASS = DB_PASSWORD


# Per-worker psycopg2 pool. Prior code opened a fresh `psycopg2.connect()` per
# request — under load, every handler paid the full TCP+TLS+auth handshake to
# managed Postgres, plus burned its 5s connect_timeout when the DB was
# contested. With 8 uvicorn workers and FastAPI dispatching sync DB calls onto
# a threadpool, that produced the "minutes to load" symptom on dashboard pages.
#
# ThreadedConnectionPool is per-process, so cluster-wide max connections are
# `workers * _POOL_MAX`. Keep _POOL_MAX small enough that 8 workers stay well
# under Azure Postgres `max_connections` (typically 100–200). minconn=1 keeps
# a warm socket per worker so the first request after idle isn't a cold start.
_POOL_MIN = 1
_POOL_MAX = 8
_pool: "ThreadedConnectionPool | None" = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not DATABASE_URL:
                    raise Exception("DATABASE_URL not configured")
                _pool = ThreadedConnectionPool(
                    minconn=_POOL_MIN,
                    maxconn=_POOL_MAX,
                    dsn=DATABASE_URL,
                    connect_timeout=5,
                )
    return _pool


class _PooledConnection:
    """Drop-in wrapper around a psycopg2 connection borrowed from the pool.

    Existing call sites do `conn = get_db_connection(); ...; conn.close()`.
    With a real pool we want `.close()` to *return* the connection rather than
    drop the socket, so callers don't pay another reconnect on the next call.
    Everything else delegates to the underlying connection.
    """

    __slots__ = ("_conn", "_pool", "_closed")

    def __init__(self, pool: ThreadedConnectionPool, conn) -> None:
        self._pool = pool
        self._conn = conn
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # Reset session state so the next borrower starts clean. Rollback
            # on an idle connection is a no-op; cursor_factory reset undoes
            # any per-borrow override (e.g. RealDictCursor below).
            self._conn.rollback()
        except Exception:
            pass
        try:
            self._conn.cursor_factory = None
        except Exception:
            pass
        try:
            self._pool.putconn(self._conn)
        except Exception:
            # Pool is closed or connection is broken — drop the socket and
            # let the pool refill on next borrow.
            try:
                self._conn.close()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, exc_type, exc, tb):
        # psycopg2 connection __exit__ commits on success / rolls back on
        # failure but does NOT close — that matches what we want before
        # returning to the pool. Caller still needs to call .close() (or use
        # this in a contextlib.closing) to actually return the connection.
        return self._conn.__exit__(exc_type, exc, tb)


def get_db_connection():
    """Borrow a psycopg2 connection from the per-worker pool.

    Caller MUST call `.close()` (or use contextlib.closing) to return the
    connection. connect_timeout=5 (set on pool init) bounds reconnect attempts
    so a flaky DB fails fast instead of hanging uvicorn workers for ~2 minutes
    on the TCP default — same v21 QA failure mode that motivated the timeout.
    """
    pool = _get_pool()
    return _PooledConnection(pool, pool.getconn())


def get_dict_cursor_connection():
    """Pooled connection whose default cursor returns dicts (RealDictCursor).

    cursor_factory is set per-borrow and reset on `.close()`, so a connection
    that gets returned to the pool here can be safely re-borrowed via
    `get_db_connection()` without leaking the dict-cursor default.
    """
    pool = _get_pool()
    conn = pool.getconn()
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return _PooledConnection(pool, conn)

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
