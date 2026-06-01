# usage: APIFY_API_TOKEN=... python apps/api/scripts/probe_apify_otw.py [linkedin_url]
import os
import sys
import json
import asyncio
import httpx

ACTOR = "freshdata/linkedin-open-to-work-status"
DEFAULT_URL = "https://www.linkedin.com/in/akarshagrawal"


async def main() -> None:
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        print("ERROR: APIFY_API_TOKEN env var not set", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    endpoint = (
        f"https://api.apify.com/v2/acts/{ACTOR.replace('/', '~')}"
        f"/run-sync-get-dataset-items?token={token}"
    )

    print(f"-> POST {endpoint.split('?')[0]}")
    print(f"-> body: {{'linkedin_url': {url!r}}}")

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(endpoint, json={"linkedin_url": url})
        print(f"<- status {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

    print("---- raw response ----")
    print(json.dumps(data, indent=2))
    print("---- parsed ----")
    try:
        otw = data[0]["data"]["open_to_work"]
        print(f"open_to_work = {otw}")
    except (KeyError, IndexError, TypeError) as e:
        print(f"could not parse items[0].data.open_to_work: {e}")


if __name__ == "__main__":
    asyncio.run(main())
