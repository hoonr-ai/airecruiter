import networkx as nx
import psycopg2
from typing import List, Dict, Set, Optional
import os

class SkillsGraph:
    def __init__(self):
        self.graph = nx.DiGraph()
        self.alias_map = {} # Loaded from DB: alias -> skill_id
        # We don't build graph on init anymore, we wait for explicit load
        
    def load_from_db(self, db_url: str = None):
        """
        Connects to Postgres and populates the graph/alias maps.
        """
        if not db_url:
            # Use SUPABASE_DB_URL for PostgreSQL connection, not SUPABASE_URL (HTTP API)
            db_url = os.getenv("SUPABASE_DB_URL")
            
        if not db_url and not os.getenv("CLOUDSQL_CONNECTION_NAME"):
            print("⚠️ Ontology: No DB configuration found (SUPABASE_DB_URL or Cloud SQL), graph will be empty.")
            return

        print("🔗 Ontology: Connecting to Database...")
        conn = None
        connector = None
        
        try:
            # Check for Cloud SQL Configuration
            cloud_sql_instance = os.getenv("CLOUDSQL_CONNECTION_NAME")
            if cloud_sql_instance:
                print(f"🔗 Ontology: Using Cloud SQL Connector for {cloud_sql_instance}...")
                from google.cloud.sql.connector import Connector, IPTypes
                import pg8000

                # Initialize Connector (requires Application Default Credentials in env)
                connector = Connector()

                def getconn():
                    return connector.connect(
                        cloud_sql_instance,
                        "pg8000",
                        user=os.getenv("DB_USER", "postgres"),
                        password=os.getenv("DB_PASSWORD", ""),
                        db=os.getenv("DB_NAME", "postgres"),
                        ip_type=IPTypes.PUBLIC  # Use PUBLIC unless we are in VPC
                    )

                # Create connection pool or single connection (using pg8000 driver)
                # Note: pg8000 is pure python, usually easier with connector
                import sqlalchemy
                pool = sqlalchemy.create_engine(
                    "postgresql+pg8000://",
                    creator=getconn,
                )
                conn = pool.raw_connection()
                
            else:
                # Fallback to Standard URL (Supabase/Postgres)
                conn = psycopg2.connect(db_url)

            cur = conn.cursor()
            
            # 1. Load Nodes
            cur.execute("SELECT slug FROM skill_nodes")
            nodes = [r[0] for r in cur.fetchall()]
            self.graph.add_nodes_from(nodes)
            print(f"✅ Ontology: Loaded {len(nodes)} skills.")
            
            # 2. Load Edges
            cur.execute("SELECT source_slug, target_slug, relation_type, weight FROM skill_edges")
            edges = cur.fetchall()
            for src, tgt, rel, w in edges:
                # Add edge with type/weight
                # 'parent_of' means src implies tgt? Or src is parent of tgt?
                # In our seed: Javascript -> React (Parent -> Child)
                # Relation: 'parent_of'
                # Logic: If I have Child (React), do I know Parent (JS)? YES.
                # So Graph Edge for scoring: Child -> Parent (React -> JS).
                # Wait, our seed says: ('javascript', 'react', 'parent_of')
                # This means JS is parent of React.
                # Use case: Candidate has React. Req is JS. Match? Yes.
                # Path: React -> JS.
                # So we want edges in the graph to flow from Child to Parent for "Implication".
                
                if rel == 'parent_of':
                    # src=JS, tgt=React.
                    # We want React -> JS (Child implies Parent).
                    self.graph.add_edge(tgt, src, type='implies') 
                    
                elif rel == 'alternative_to':
                    # bi-directional
                    self.graph.add_edge(src, tgt, type='alternative', weight=w)
                    self.graph.add_edge(tgt, src, type='alternative', weight=w)
                    
            print(f"✅ Ontology: Loaded {len(edges)} relations.")

            # 3. Load Aliases
            cur.execute("SELECT alias, skill_id FROM skill_aliases")
            aliases = cur.fetchall()
            count = 0
            for alias, skill_id in aliases:
                # Store normalized alias for robust lookup
                # e.g. "next.js" -> "nextjs" or keep as is?
                # Best to store raw from DB, and caller normalizes input before lookup?
                # Let's trust the DB has the "lookup key" we expect.
                # Actually, our utils.normalize_skill produces "next_js".
                # If DB has "next.js", we might miss it if we normalize "Next.js" -> "next_js".
                # Solution: normalize the key in the map.
                from core.utils import normalize_skill
                norm_alias = normalize_skill(alias)
                self.alias_map[norm_alias] = skill_id
                count += 1
            print(f"✅ Ontology: Loaded {count} aliases.")

            cur.close()
            conn.close()
            
        except psycopg2.errors.UndefinedTable:
            print(f"❌ Ontology Load Failed: Table 'skill_nodes' not found in database '{os.getenv('DB_NAME')}'.")
            try:
                # Debug: List available databases
                cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false;")
                dbs = [row[0] for row in cur.fetchall()]
                print(f"ℹ️  Available Databases: {dbs}")
                print("👉 Please update DB_NAME in .env to one of the above.")
            except Exception as e2:
                print(f"⚠️ Failed to list databases: {e2}")

        except Exception as e:
            print(f"❌ Ontology Load Failed: {e}")
            # Fallback to empty?

    def resolve_alias(self, skill_name_or_slug: str) -> str:
        """
        Resolves a potential alias to the canonical skill slug.
        e.g. "ReactJS" -> "react"
        """
        from core.utils import normalize_skill
        # 1. Normalize input string
        s = normalize_skill(skill_name_or_slug)
        
        # 2. Check Alias Map
        if s in self.alias_map:
            return self.alias_map[s]
            
        # 3. Check if it's already a valid node
        if s in self.graph:
            return s
            
        # 4. Fallback (return normalized)
        return s

    def get_implied_skills(self, skill_id: str) -> Set[str]:
        if skill_id not in self.graph:
            return set()
        
        implied = set()
        for neighbor in self.graph.neighbors(skill_id):
            if self.graph[skill_id][neighbor].get('type') == 'implies':
                implied.add(neighbor)
        return implied

    def get_related_scores(self, req_skill: str, candidate_skills: Set[str]) -> float:
        """
        Calculates the best match score for a required skill against a candidate's skill set.
        """
        # Resolve Request Alias (just in case)
        req_canonical = self.resolve_alias(req_skill)
        
        # Pre-resolve candidate skills? Or assume caller did?
        # Caller (engine) usually passes normalize_skill(id). 
        # But now normalize_skill DOES NOT alias map.
        # So we should resolve candidate skills here or ensure caller does.
        # Efficiently: Caller should resolve. But for safety, let's assume they are keys.
        
        if req_canonical in candidate_skills:
            return 1.0
            
        max_score = 0.0
        
        for cand_skill in candidate_skills:
            # Cand skill should also be resolved if not already?
            # Assuming candidate_skills are already resolved slugs.
            
            if cand_skill not in self.graph: 
                continue
                
            # 1. Implication (Cand -> Req)
            if self.graph.has_edge(cand_skill, req_canonical):
                edge_data = self.graph[cand_skill][req_canonical]
                if edge_data.get('type') == 'implies':
                    return 1.0 
            
            # 2. Alternatives
            if self.graph.has_edge(cand_skill, req_canonical):
                edge_data = self.graph[cand_skill][req_canonical]
                if edge_data.get('type') == 'alternative':
                    max_score = max(max_score, edge_data.get('weight', 0.5))
                    
        return max_score

# Singleton Instance
ontology = SkillsGraph()
