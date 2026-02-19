import asyncio
import os
import sys

# Add parent directory to path to import services
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.jobdiva import JobDivaService

async def test_search():
    print("🚀 Starting JobDiva Search Test (Round 5)...")
    
    try:
        service = JobDivaService()
        token = await service.authenticate()
        if not token:
            print("❌ Authentication Failed!")
            return
        print("✅ Authenticated")
    except Exception as e:
        print(f"❌ Setup Failed: {e}")
        return

    # Define Test Cases
    tests = [
        {
             "name": "Exp 6a: Minimal Synonyms",
             "payload": {
                 "skills": ['"SQL" AND ("Data Analysis" OR "Data Analytics")'],
                 "pageNumber": 1,
                 "pageSize": 5
             }
        },
        {
             "name": "Exp 6b: With Data Science",
             "payload": {
                 "skills": ['"SQL" AND ("Data Analysis" OR "Data Analytics" OR "Data Science")'],
                 "pageNumber": 1,
                 "pageSize": 5
             }
        },
        {
             "name": "Exp 6c: SQL Variation",
             "payload": {
                 "skills": ['("SQL" OR "MySQL") AND "Data Analysis"'],
                 "pageNumber": 1,
                 "pageSize": 5
             }
        }
    ]
    
    import httpx
    url = f"{service.api_url}/apiv2/jobdiva/TalentSearch"
    headers = {"Authorization": f"Bearer {token}"}

    for t in tests:
        print(f"\n🧪 Testing: {t['name']}")
        print(f"📦 Payload: {t['payload']}")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=t['payload'], headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = []
                    total = "Unknown"
                    
                    if isinstance(data, list):
                        candidates = data
                        total = len(data)
                        print(f"⚠️ Response is LIST.")
                    else:
                        candidates = data.get("candidates", [])
                        total = data.get("totalCount", "Unknown")
                        
                    count = len(candidates)
                    print(f"✅ Status: 200 | Candidates Fetched: {count}")
                    if count > 0:
                        first = candidates[0]
                        print(f"📄 Sample: {first}")
                else:
                    print(f"❌ Status: {resp.status_code} | Body: {resp.text}")
        except Exception as e:
            print(f"❌ Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_search())
