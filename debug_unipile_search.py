import asyncio
import sys
import os

# Dynamically add apps/api to path
api_path = os.path.join(os.path.dirname(__file__), "apps", "api")
if api_path not in sys.path:
    sys.path.append(api_path)

from apps.api.services.unipile import UnipileService

async def test_search():
    service = UnipileService()
    account_id = await service.get_account_id()
    print(f"Account ID: {account_id}")
    
    if not account_id:
        print("No LinkedIn account connected.")
        return

    # Test Case 1: Simple Skill + Location (San Francisco)
    skills = ["Product Manager"]
    location = "San Francisco, CA"
    
    print(f"\n--- TEST 1: Skills={skills}, Location='{location}' ---")
    results = await service.search_candidates(skills, location, open_to_work=False, limit=5)
    print(f"Results: {len(results)}")
    for c in results:
        print(f" - {c['firstName']} {c['lastName']} ({c['city']})")

    # Test Case 2: Skill + Location (New York) + OpenToWork
    skills2 = ["Python"]
    location2 = "New York, NY"
    print(f"\n--- TEST 2: Skills={skills2}, Location='{location2}', OpenToWork=True ---")
    results2 = await service.search_candidates(skills2, location2, open_to_work=True, limit=5)
    print(f"Results: {len(results2)}")
    for c in results2:
        print(f" - {c['firstName']} {c['lastName']} ({c['city']})")

if __name__ == "__main__":
    asyncio.run(test_search())
