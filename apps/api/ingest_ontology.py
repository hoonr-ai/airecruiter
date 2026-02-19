
import os
import csv
import logging
from typing import List, Tuple, Set
from google.cloud.sql.connector import Connector, IPTypes
import sqlalchemy

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CSV_PATH = "ontology_data.csv"

def get_db_connection():
    """Establishes a connection to the Cloud SQL database."""
    instance_connection_name = os.getenv("CLOUDSQL_CONNECTION_NAME")
    db_user = os.getenv("DB_USER", "postgres")
    db_pass = os.getenv("DB_PASSWORD", "password")
    db_name = os.getenv("DB_NAME", "postgres")

    if not instance_connection_name:
        raise ValueError("CLOUDSQL_CONNECTION_NAME environment variable not set")

    connector = Connector()

    def getconn():
        return connector.connect(
            instance_connection_name,
            "pg8000",
            user=db_user,
            password=db_pass,
            db=db_name,
            ip_type=IPTypes.PUBLIC
        )

    pool = sqlalchemy.create_engine(
        "postgresql+pg8000://",
        creator=getconn,
    )
    return pool

def normalize_slug(text: str) -> str:
    """Normalizes a string to a slug format (lowercase, underscores)."""
    return text.lower().strip().replace(" ", "_").replace("/", "_").replace("-", "_")

def ingest_taxonomy(file_path: str):
    """
    Parses the Taxonomy CSV and ingests nodes and edges.
    
    Schema Assumption:
    Columns range from Specific (Left) to Generic (Right).
    ROLE_K17000 (Specific) -> ... -> ROLE_K10 (Generic)
    
    Relationship:
    Generic IS PARENT OF Specific.
    (Technician) -> parent_of -> (Dairy Processing Technician)
    
    This matches our logic: Child (Dairy Tech) IMPLIES Parent (Technician).
    """
    
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return

    nodes: Set[str] = set()
    edges: Set[Tuple[str, str, str]] = set() # (Source, Target, Type)

    logger.info("Parsing CSV...")
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader) # ROLE_K17000 ... ROLE_K10
        
        # Identify column indices
        # We expect them to be roughly ordered, but let's just treat adjacent columns as relationships.
        # Flow: index i -> index i+1
        # If col[i] != col[i+1], then col[i+1] is PARENT of col[i]
        # Wait, header suggests reverse? 
        # Line 2: Dairy Processing Technician (0) ... Technician (8)
        # So Index 0 is Specific, Index 8 is Generic.
        # So Col[i+1] (More Generic) is Parent of Col[i] (More Specific).
        
        for row in reader:
            # Clean row
            cleaned_row = [x.strip() for x in row if x.strip()]
            
            # Iterate through pairs
            for i in range(len(cleaned_row) - 1):
                child_term = cleaned_row[i]
                parent_term = cleaned_row[i+1]
                
                if not child_term or not parent_term: continue
                if child_term == parent_term: continue # Skip self-loops
                
                child_slug = normalize_slug(child_term)
                parent_slug = normalize_slug(parent_term)
                
                nodes.add(child_slug)
                nodes.add(parent_slug)
                
                # Edge: Parent -> Child (parent_of)
                edges.add((parent_slug, child_slug, 'parent_of'))

    logger.info(f"Parsed {len(nodes)} Unique Nodes")
    logger.info(f"Parsed {len(edges)} Unique Edges")
    
    # Database Operations
    db = get_db_connection()
    conn = db.connect()
    trans = conn.begin()
    
    try:
        # 1. Insert Nodes
        logger.info("Inserting Nodes...")
        # Use ON CONFLICT DO NOTHING
        # Batch insert? SQLAlchemy has limitations with RETURNING in strict mode, let's use raw execution for speed
        
        # Prepare list for executemany
        node_data = [{"slug": n, "name": n.replace("_", " ").title(), "category": "imported"} for n in nodes]
        
        # Chunking to avoid packet size issues
        chunk_size = 1000
        for i in range(0, len(node_data), chunk_size):
            chunk = node_data[i:i+chunk_size]
            conn.execute(
                sqlalchemy.text("INSERT INTO skill_nodes (slug, name, category) VALUES (:slug, :name, :category) ON CONFLICT (slug) DO NOTHING"),
                chunk
            )
            
        logger.info("Nodes Inserted.")

        # 2. Insert Edges
        logger.info("Inserting Edges...")
        edge_data = [{"src": s, "tgt": t, "rel": r, "w": 1.0} for s, t, r in edges]
        
        for i in range(0, len(edge_data), chunk_size):
            chunk = edge_data[i:i+chunk_size]
            conn.execute(
                sqlalchemy.text("INSERT INTO skill_edges (source_slug, target_slug, relation_type, weight) VALUES (:src, :tgt, :rel, :w) ON CONFLICT (source_slug, target_slug) DO NOTHING"),
                chunk
            )
            
        logger.info("Edges Inserted.")
        
        trans.commit()
        logger.info("✅ Ingestion Complete Successfully.")
        
    except Exception as e:
        trans.rollback()
        logger.error(f"❌ Ingestion Failed: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    ingest_taxonomy(CSV_PATH)
