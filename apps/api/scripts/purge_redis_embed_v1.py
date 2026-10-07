"""Delete legacy `llm:embed:v1:*` keys from Redis.

v1 embeddings were stored as JSON with no TTL, so `volatile-lru` could
never evict them and the instance filled up (every other cache write then
failed with `OOM command not allowed`). v2 keys carry a TTL; v1 keys are
dead weight once the new code is deployed.

Dry run (counts only):
    cd apps/api && python -m scripts.purge_redis_embed_v1
Delete:
    cd apps/api && python -m scripts.purge_redis_embed_v1 --apply

Optional: --url redis://... (defaults to REDIS_URL from the environment).
"""

from __future__ import annotations

import argparse
import os
import sys

import redis

_PATTERN = "llm:embed:v1:*"
_BATCH = 500


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=None, help="Redis URL (default: REDIS_URL)")
    parser.add_argument("--apply", action="store_true", help="Actually delete keys")
    args = parser.parse_args()

    url = args.url
    if not url:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        url = os.getenv("REDIS_URL", "")
    if not url:
        print("REDIS_URL not set; pass --url")
        return 1

    client = redis.from_url(url, socket_connect_timeout=5, socket_timeout=30)
    before = client.info("memory").get("used_memory_human")

    matched = 0
    deleted = 0
    batch: list = []
    for key in client.scan_iter(match=_PATTERN, count=1000):
        matched += 1
        if not args.apply:
            continue
        batch.append(key)
        if len(batch) >= _BATCH:
            deleted += client.unlink(*batch)
            batch.clear()
    if args.apply and batch:
        deleted += client.unlink(*batch)

    after = client.info("memory").get("used_memory_human")
    mode = "deleted" if args.apply else "would delete (dry run)"
    print(f"{_PATTERN}: matched={matched} {mode}={deleted if args.apply else matched}")
    print(f"used_memory: {before} -> {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
