import asyncio
import os
import sys

# Add parent directory to path to import services
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.jobdiva import JobDivaService

async def test_job_fetch():
    print("🚀 Starting JobDiva Job Fetch Test...")
    
    # Load .env manually if needed, but JobDivaService should pick it up if running from root or if env vars are set
    # Using python-dotenv just in case
    from dotenv import load_dotenv
    load_dotenv("apps/api/.env")

    try:
        service = JobDivaService()
        token = await service.authenticate()
        if not token:
            print("❌ Authentication Failed!")
            return
        
        # Check if we are mock or real
        if token == "mock-token-123":
             print("⚠️  WARNING: Running in MOCK mode. Credentials might be missing.")
        else:
             print("✅ Authenticated with REAL token.")

    except Exception as e:
        print(f"❌ Setup Failed: {e}")
        return

    # Test Case: The Job ID user mentioned earlier or a known text example
    # User said: "Job 26-01904"
    test_ids = ["26-01904", "2601904"] 

    for jid in test_ids:
        print(f"\n🧪 Fetching Job ID: {jid}")
        job = await service.get_job_by_id(jid)
        
        if job:
            print(f"✅ Found Job!")
            print(f"   Title: {job.get('title')}")
            print(f"   Company: {job.get('company')}")
            print(f"   Description Length: {len(job.get('description', ''))}")
        else:
            print(f"❌ Job {jid} NOT FOUND.")

if __name__ == "__main__":
    asyncio.run(test_job_fetch())
