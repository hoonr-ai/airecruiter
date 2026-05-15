import logging
import json
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from typing import List, Dict, Any, Optional

from core.db import get_db_connection

logger = logging.getLogger(__name__)

_METRICS_ZERO = {
    "candidates_sourced": 0,
    "candidates_launched": 0,
    "complete_submissions": 0,
    "pass_submissions": 0,
}

class MetricsService:
    """Service to handle recruitment metrics aggregation and caching in monitored_jobs."""

    def _aggregate_candidate_metrics(self, cursor, unique_keys: List[str]) -> Dict[str, Dict[str, int]]:
        """Aggregates metrics for a set of job keys."""
        if not unique_keys:
            return {}
            
        cursor.execute(
            """
            SELECT
                jobdiva_id,
                COUNT(DISTINCT candidate_id)                                              AS candidates_sourced,
                COUNT(DISTINCT CASE 
                    WHEN (data->>'engage_status') IS NOT NULL 
                    THEN candidate_id 
                END)                                                                       AS candidates_launched,
                COUNT(DISTINCT CASE
                    WHEN data->>'engage_status' IN ('completed', 'failed', 'passed', 'rejected', 'pass', 'fail')
                    THEN candidate_id
                END)                                                                       AS complete_submissions,
                COUNT(DISTINCT CASE
                    WHEN (data->>'engage_status' IN ('passed', 'pass', 'completed'))
                      OR (LOWER(data->>'engage_hard_filter_status') IN ('pass', 'passed')
                          AND (NULLIF(data->>'engage_score', '')::float >= 70))
                    THEN candidate_id
                END)                                                                       AS pass_submissions
            FROM sourced_candidates
            WHERE jobdiva_id = ANY(%s)
            GROUP BY jobdiva_id
            """,
            (unique_keys,),
        )
        
        out: Dict[str, Dict[str, int]] = {}
        for row in cursor.fetchall() or []:
            out[str(row[0])] = {
                "candidates_sourced": int(row[1] or 0),
                "candidates_launched": int(row[2] or 0),
                "complete_submissions": int(row[3] or 0),
                "pass_submissions": int(row[4] or 0),
            }
        return out

    def _sum_metrics_for_job(
        self,
        metrics_by_key: Dict[str, Dict[str, int]],
        jobdiva_id: Any,
        job_id: Any,
    ) -> Dict[str, int]:
        summed = dict(_METRICS_ZERO)
        candidates = []
        if jobdiva_id is not None and str(jobdiva_id) != "":
            candidates.append(str(jobdiva_id))
        if job_id is not None and str(job_id) != "":
            candidates.append(str(job_id))
        for key in candidates:
            m = metrics_by_key.get(key)
            if m:
                for field in summed:
                    summed[field] += m[field]
        return summed

    def refresh_job_metrics(self, job_id_or_jobdiva_id: str) -> None:
        """Recalculate and update metrics for a single job."""
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1. Resolve IDs
                cur.execute("SELECT job_id, jobdiva_id FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s LIMIT 1", 
                           (str(job_id_or_jobdiva_id), str(job_id_or_jobdiva_id)))
                job = cur.fetchone()
                if not job:
                    return

                jid, jdid = job["job_id"], job["jobdiva_id"]
                keys = list(set(filter(None, [str(jid), str(jdid)])))
                
                # 2. Aggregate
                metrics = self._aggregate_candidate_metrics(cur, keys)
                stats = self._sum_metrics_for_job(metrics, jdid, jid)
                
                # 3. Update
                cur.execute("""
                    UPDATE monitored_jobs SET
                        candidates_sourced = %s,
                        candidates_launched = %s,
                        complete_submissions = %s,
                        pass_submissions = %s
                    WHERE job_id = %s
                """, (
                    stats["candidates_sourced"],
                    stats["candidates_launched"],
                    stats["complete_submissions"],
                    stats["pass_submissions"],
                    jid
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to refresh metrics for job {job_id_or_jobdiva_id}: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


    def refresh_all_active_metrics(self) -> None:
        """Recalculate and update metrics for all non-archived jobs in a single batch update."""
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT job_id, jobdiva_id FROM monitored_jobs WHERE is_archived IS NOT TRUE")
                jobs = cur.fetchall()
                if not jobs:
                    return

                # Process in batches of 10 to prevent 100% CPU spikes on smaller databases (like QA)
                batch_size = 10
                for i in range(0, len(jobs), batch_size):
                    batch = jobs[i:i + batch_size]
                    
                    batch_keys = []
                    for j in batch:
                        if j["jobdiva_id"]: batch_keys.append(str(j["jobdiva_id"]))
                        if j["job_id"]: batch_keys.append(str(j["job_id"]))
                    
                    if not batch_keys:
                        continue
                        
                    # 1. Aggregate metrics in bulk for this batch
                    metrics = self._aggregate_candidate_metrics(cur, list(set(batch_keys)))
                    
                    # 2. Prepare data for bulk update
                    update_data = []
                    for j in batch:
                        jid, jdid = j["job_id"], j["jobdiva_id"]
                        stats = self._sum_metrics_for_job(metrics, jdid, jid)
                        update_data.append((
                            jid,
                            stats["candidates_sourced"],
                            stats["candidates_launched"],
                            stats["complete_submissions"],
                            stats["pass_submissions"]
                        ))

                    # 3. Perform bulk update using execute_values
                    if update_data:
                        execute_values(cur, """
                            UPDATE monitored_jobs AS mj SET
                                candidates_sourced = stats.cs,
                                candidates_launched = stats.cl,
                                complete_submissions = stats.cms,
                                pass_submissions = stats.ps
                            FROM (VALUES %s) AS stats(jid, cs, cl, cms, ps)
                            WHERE mj.job_id = stats.jid::uuid
                        """, update_data)
                        
                        conn.commit()
                        
            logger.info(f"📊 Global recruitment metrics refresh complete for {len(jobs)} jobs")
        except Exception as e:
            logger.error(f"Failed global metrics refresh: {e}", exc_info=True)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

metrics_service = MetricsService()
