"""Look up PAIR call recordings in S3 and hand out short-lived playback URLs.

pair-bot writes one object per LiveKit job at

    {S3_RECORDINGS_PREFIX}{YYYY-MM-DD}/{jobdiva_id}/{shard}/{session_id}_{lk_job_id}.ogg

where the date is the UTC day the session started, `jobdiva_id` is
`no-jobdiva` when the interview has no JobDiva job, `shard` is
`session_id // 1000` zero-padded to 4 digits, and every key part is
sanitised to `[A-Za-z0-9._-]`. Recordings only reach S3 when pair-bot runs
with ENVIRONMENT=production, so the lookup is disabled everywhere else.
"""

import functools
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Short-lived: the modal asks for fresh links every time it opens.
PRESIGN_TTL_SECONDS = 900
LISTING_CACHE_SECONDS = 60
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_listing_cache: Dict[Tuple, Tuple[float, List[Dict[str, Any]]]] = {}
_listing_lock = threading.Lock()


def _bucket() -> str:
    return os.getenv("S3_RECORDINGS_BUCKET", "").strip()


def _prefix() -> str:
    return os.getenv("S3_RECORDINGS_PREFIX", "recordings/")


def is_enabled() -> bool:
    return os.getenv("ENVIRONMENT", "").strip().lower() == "production" and bool(_bucket())


@functools.lru_cache(maxsize=1)
def _s3():
    import boto3  # imported lazily: only production needs it

    return boto3.client("s3", region_name=os.getenv("AWS_REGION") or None)


def _part(value: Any) -> str:
    return _UNSAFE_KEY_CHARS.sub("_", str(value))


def _utc_day(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _session_prefixes(
    session_id: int, jobdiva_id: Optional[str], started_at: Any, fallback_day: Optional[date]
) -> List[str]:
    jd = _part(jobdiva_id) if jobdiva_id else "no-jobdiva"
    shard = f"{session_id // 1000:04d}"
    day = _utc_day(started_at)
    if day:
        days: Iterable[str] = [day.isoformat()]
    elif fallback_day:
        # Session start unknown: the folder is `unknown-date` or near the
        # interview's creation day.
        days = ["unknown-date"] + [(fallback_day + timedelta(d)).isoformat() for d in range(-1, 4)]
    else:
        days = ["unknown-date"]
    return [f"{_prefix()}{d}/{jd}/{shard}/{session_id}" for d in days]


def _list_session_objects(s3, bucket: str, prefix: str, session_id: int) -> List[Dict[str, Any]]:
    """Objects of one session under `prefix` (ends with the session id).

    The listing is by bare prefix, so the key's file name is checked to keep
    session 607 from matching 6074: it must be `{id}.ogg` or `{id}_{job}.ogg`.
    """
    name_re = re.compile(rf"^{session_id}(?:_[^/]*)?\.ogg$")
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if name_re.match(obj["Key"].rsplit("/", 1)[-1]):
                objects.append(obj)
    return objects


def _find_keys(
    sessions: List[Dict[str, Any]], jobdiva_id: Optional[str], fallback_day: Optional[date]
) -> List[Dict[str, Any]]:
    cache_key = (
        tuple((str(x.get("id")), str(x.get("started_at"))) for x in sessions),
        jobdiva_id,
        fallback_day,
    )
    now = time.monotonic()
    with _listing_lock:
        hit = _listing_cache.get(cache_key)
        if hit and now - hit[0] < LISTING_CACHE_SECONDS:
            return hit[1]

    s3 = _s3()
    bucket = _bucket()
    found: Dict[str, Dict[str, Any]] = {}
    for session in sessions:
        try:
            session_id = int(session["id"])
        except (KeyError, TypeError, ValueError):
            continue
        for prefix in _session_prefixes(session_id, jobdiva_id, session.get("started_at"), fallback_day):
            for obj in _list_session_objects(s3, bucket, prefix, session_id):
                found[obj["Key"]] = {
                    "key": obj["Key"],
                    "session_id": session_id,
                    "size": obj["Size"],
                    "recorded_at": obj["LastModified"].isoformat(),
                    "started_at": session.get("started_at"),
                }
    result = sorted(found.values(), key=lambda r: (r["started_at"] or "", r["recorded_at"]))
    with _listing_lock:
        if len(_listing_cache) > 256:
            _listing_cache.clear()
        _listing_cache[cache_key] = (now, result)
    return result


def find_recordings(
    sessions: List[Dict[str, Any]], jobdiva_id: Optional[str], fallback_day: Optional[date] = None
) -> Dict[str, Any]:
    """Recordings for the given PAIR sessions ({id, started_at}), oldest first.

    Returns {"recordings": [...], "unavailable": bool}. S3 failures (missing
    credentials, AccessDenied, throttling) are logged and reported as
    `unavailable` rather than raised, so a recordings outage never looks like
    a failure of the API itself.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        recordings = [dict(r) for r in _find_keys(sessions, jobdiva_id, fallback_day)]
        s3 = _s3()
        for rec in recordings:
            rec["url"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": _bucket(), "Key": rec["key"], "ResponseContentType": "audio/ogg"},
                ExpiresIn=PRESIGN_TTL_SECONDS,
            )
        return {"recordings": recordings, "unavailable": False}
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "S3 recordings lookup failed (bucket=%s prefix=%s): %s", _bucket(), _prefix(), exc
        )
        return {"recordings": [], "unavailable": True}


def _mock_flag_set() -> bool:
    return os.getenv("MOCK_CALL_RECORDINGS", "").strip().lower() in {"1", "true", "yes", "on"}


def mock_enabled() -> bool:
    """Dev-only: show a fake recording so the player can be seen without S3."""
    return _mock_flag_set() and os.getenv("ENVIRONMENT", "").strip().lower() != "production"


def warn_if_misconfigured() -> None:
    """Log loudly if MOCK_CALL_RECORDINGS is set alongside ENVIRONMENT=production.

    `mock_enabled()` already refuses to serve mock audio in that case, so this
    is not a security hole — but a deploy with both set almost certainly has
    a stray dev flag left on, and that should show up at startup rather than
    stay silent.
    """
    if _mock_flag_set() and os.getenv("ENVIRONMENT", "").strip().lower() == "production":
        logger.warning(
            "MOCK_CALL_RECORDINGS is set in a production environment — it has no "
            "effect here (mock recordings are disabled in production), but this "
            "flag should not be set in prod config."
        )


def mock_recordings() -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": "mock-recording",
            "session_id": 0,
            "size": 0,
            "recorded_at": now,
            "started_at": now,
            "url": "/api/v1/engagement/mock-recording.wav",
        }
    ]


def mock_wav(seconds: int = 8) -> bytes:
    """A quiet 8s two-tone WAV, generated in memory."""
    import io
    import math
    import struct
    import wave

    rate = 8000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(rate * seconds):
            freq = 440 if (i // rate) % 2 == 0 else 330
            frames += struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()
