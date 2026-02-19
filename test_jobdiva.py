
import os
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv("apps/api/.env")

USERNAME = os.getenv("JOBDIVA_USERNAME")
PASSWORD = os.getenv("JOBDIVA_PASSWORD")
CLIENT_ID = os.getenv("JOBDIVA_CLIENT_ID")
API_URL = os.getenv("JOBDIVA_API_URL", "https://api.jobdiva.com")

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
