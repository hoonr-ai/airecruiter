import asyncio
import httpx
import os
import sys

# Dynamically add apps/api to path
api_path = os.path.join(os.path.dirname(__file__), "apps", "api")
if api_path not in sys.path:
    sys.path.append(api_path)

from core.config import UNIPILE_API_KEY, UNIPILE_DSN

API_KEY = UNIPILE_API_KEY
DSN = UNIPILE_DSN
if not DSN.startswith("http"): DSN = f"https://{DSN}"
API_URL = f"{DSN}/api/v1"

# Known ID (Gerda Brunner)
TEST_ID = "ACoAAB6ztWUBvveMP-s9WjhSPtDWQbPGu5jNXWY"

async def test_endpoints():
    if not API_KEY:
        print("❌ No API Key Found")
        return

    # Get Account ID first
    account_id = None
    headers = {"X-API-KEY": API_KEY, "Accept": "application/json"}
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Get Account
        resp = await client.get(f"{API_URL}/accounts", headers=headers)
        if resp.status_code == 200:
            for acc in resp.json().get("items", []):
                if acc.get("type", "").upper() == "LINKEDIN" and acc.get("status") == "OK":
                    account_id = acc.get("id")
                    break
    
    if not account_id:
        print("❌ No LinkedIn Account Found")
        return
        
    print(f"✅ Using Account ID: {account_id}")
    
    # Try Endpoints
    endpoints = [
        f"/linkedin/users/{TEST_ID}",
        f"/users/{TEST_ID}",
        f"/linkedin/profiles/{TEST_ID}",
        f"/profiles/{TEST_ID}",
        f"/people/{TEST_ID}",
        f"/linkedin/people/{TEST_ID}"
    ]
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        for ep in endpoints:
            url = f"{API_URL}{ep}"
            print(f"Testing {url}...")
            try:
                resp = await client.get(url, params={"account_id": account_id}, headers=headers)
                print(f"Result: {resp.status_code}")
                if resp.status_code == 200:
                    print("✅ FOUND IT!")
                    print(str(resp.json())[:200])
                    break
            except Exception as e:
                print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_endpoints())
