
import asyncio
import os
import sys
# Add current dir to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

from services.unipile import UnipileService

async def test_unipile():
    print("--- Testing Unipile Service ---")

    service = UnipileService()
    
    # 1. Check Account
    print("1. Fetching LinkedIn Account ID...")
    account_id = await service.get_account_id()
    if not account_id:
        print("❌ Failed to get Account ID. Check API Key or Connections.")
        return

    print(f"✅ Account ID Found: {account_id}")

    # 2. Search
    print("\n2. Searching for 'Java' + Open to Work (Limit 5)...")
    results = await service.search_candidates(["Java"], "San Francisco", open_to_work=True, limit=5)
    
    print(f"✅ Found {len(results)} Candidates:")
    for c in results:
        print(f"   - {c['firstName']} {c['lastName']} ({c['title']})")
        print(f"     OpenToWork: {c['open_to_work']}")
        print(f"     Profile: {c['profile_url']}")
        print("---")

    # 3. Message Function Test (Draft only - don't actually send unless user wants)
    # We will just verify we have the ID needed to message.
    if results:
        target = results[0]
        print(f"\n3. Ready to Message: {target['firstName']} (Provider ID: {target['provider_id']})")
        # await service.send_message(target['provider_id'], "Test Message") # Commented out for safety

if __name__ == "__main__":
    asyncio.run(test_unipile())
