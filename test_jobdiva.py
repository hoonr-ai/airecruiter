import sys
import os
import asyncio
import httpx

# Dynamically add apps/api to path
api_path = os.path.join(os.path.dirname(__file__), "apps", "api")
if api_path not in sys.path:
    sys.path.append(api_path)

from core.config import (
    JOBDIVA_API_URL, JOBDIVA_CLIENT_ID, 
    JOBDIVA_USERNAME, JOBDIVA_PASSWORD
)

# Use imported config
API_URL = JOBDIVA_API_URL
CLIENT_ID = JOBDIVA_CLIENT_ID
USERNAME = JOBDIVA_USERNAME
PASSWORD = JOBDIVA_PASSWORD

print(f"Testing JobDiva Auth with User: {USERNAME}, ClientID: {CLIENT_ID}")

async def test_auth():
    auth_url = f"{API_URL}/api/authenticate"
    params = {
        "clientid": CLIENT_ID,
        "username": USERNAME,
        "password": PASSWORD
    }
    
    try:
        async with httpx.AsyncClient() as client:
            print("Sending request...")
            resp = await client.get(auth_url, params=params)
            print(f"Status Code: {resp.status_code}")
            print(f"Response Text: {resp.text}")
            
            if resp.status_code == 200:
                token = resp.text.replace('"', '')
                print(f"Success! Token: {token[:10]}...")
            else:
                print("Authentication Failed.")

    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_auth())
