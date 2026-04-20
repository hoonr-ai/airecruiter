import asyncio
import logging
import sys
import os
from datetime import datetime, timezone, timedelta

# Add current directory to path so we can import services
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from services.auto_assign_service import auto_assign_service
import psycopg2
from psycopg2.extras import RealDictCursor
from core.config import DATABASE_URL

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("jobdiva_applicant_auto_sync.log")
    ]
)
logger = logging.getLogger("JobDivaApplicantSync")

def get_monitored_jobs():
    """Fetch all jobs that are not archived and have a jobdiva_id."""
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # We target all jobs that have been 'monitored'. 
                # If there's an is_archived column, we should use it.
                # Checking schema for common column names.
                cur.execute("SELECT job_id, jobdiva_id, title FROM monitored_jobs")
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch monitored jobs: {e}")
        return []

async def run_sync_cycle():
    """Run one full sync cycle for all monitored jobs."""
    start_time = datetime.now()
    logger.info(f"🚀 Starting Sync Cycle at {start_time.isoformat()}")
    
    jobs = get_monitored_jobs()
    if not jobs:
        logger.info("No jobs found to sync.")
        return

    logger.info(f"Found {len(jobs)} jobs to process.")
    
    for job in jobs:
        job_id = job['job_id']
        jobdiva_id = job['jobdiva_id']
        title = job.get('title', 'Unknown Title')
        
        logger.info(f"🔄 Syncing Job: {title} (ID: {job_id} / Ref: {jobdiva_id})")
        
        try:
            # We use the UUID job_id for the service call
            count = await auto_assign_service.synchronize_job_applicants(job_id)
            logger.info(f"✅ Successfully synced {count} applicants for {title}")
        except Exception as e:
            logger.error(f"❌ Error syncing job {job_id}: {e}")
            
        # Small delay between jobs to respect API rate limits and prevent LLM bursts
        await asyncio.sleep(2)

    end_time = datetime.now()
    duration = end_time - start_time
    logger.info(f"🏁 Sync Cycle Finished. Duration: {duration}")

if __name__ == "__main__":
    # This script is designed to be run once via Cron (e.g., every 15 mins)
    try:
        asyncio.run(run_sync_cycle())
    except KeyboardInterrupt:
        logger.info("Sync Agent interrupted by user.")
    except Exception as e:
        logger.critical(f"Sync Agent crashed: {e}", exc_info=True)
        sys.exit(1)
