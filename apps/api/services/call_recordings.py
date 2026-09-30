"""Look up PAIR call recordings in S3 and hand out short-lived playback URLs.

pair-bot writes one object per LiveKit job at

    {S3_RECORDINGS_PREFIX}{YYYY-MM-DD}/{jobdiva_id}/{shard}/{session_id}_{lk_job_id}.ogg

where the date is the UTC day the session started, `jobdiva_id` is
`no-jobdiva` when the interview has no JobDiva job, `shard` is
`session_id // 1000` zero-padded to 4 digits, and every key part is
sanitised to `[A-Za-z0-9._-]`. Recordings only reach S3 when pair-bot runs
with ENVIRONMENT=production, so the lookup is disabled everywhere else.
"""

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

PRESIGN_TTL_SECONDS = 3600
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_client = None


def _bucket() -> str:
    return os.getenv("S3_RECORDINGS_BUCKET", "").strip()


def _prefix() -> str:
    return os.getenv("S3_RECORDINGS_PREFIX", "recordings/")


def is_enabled() -> bool:
    return os.getenv("ENVIRONMENT", "").strip().lower() == "production" and bool(_bucket())


def _s3():
    global _client
    if _client is None:
        import boto3  # imported lazily: only production needs it

        _client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or None)
    return _client


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


def find_recordings(
    sessions: List[Dict[str, Any]], jobdiva_id: Optional[str], fallback_day: Optional[date] = None
) -> List[Dict[str, Any]]:
    """Recordings for the given PAIR sessions ({id, started_at}), oldest first."""
    s3 = _s3()
    bucket = _bucket()
    found: List[Dict[str, Any]] = []
    for session in sessions:
        try:
            session_id = int(session["id"])
        except (KeyError, TypeError, ValueError):
            continue
        for prefix in _session_prefixes(session_id, jobdiva_id, session.get("started_at"), fallback_day):
            # `{session_id}_` / `{session_id}.` so session 607 never matches 6074.
            for delim in ("_", "."):
                resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix + delim)
                for obj in resp.get("Contents", []):
                    found.append(
                        {
                            "key": obj["Key"],
                            "session_id": session_id,
                            "size": obj["Size"],
                            "recorded_at": obj["LastModified"].isoformat(),
                            "started_at": session.get("started_at"),
                        }
                    )
    found.sort(key=lambda r: (r["started_at"] or "", r["recorded_at"]))
    seen = set()
    unique = []
    for rec in found:
        if rec["key"] not in seen:
            seen.add(rec["key"])
            unique.append(rec)
    for rec in unique:
        rec["url"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": rec["key"], "ResponseContentType": "audio/ogg"},
            ExpiresIn=PRESIGN_TTL_SECONDS,
        )
    return unique


def mock_enabled() -> bool:
    """Dev-only: show a fake recording so the player can be seen without S3."""
    return (
        os.getenv("MOCK_CALL_RECORDINGS", "").strip().lower() in {"1", "true", "yes", "on"}
        and os.getenv("ENVIRONMENT", "").strip().lower() != "production"
    )


def mock_recordings() -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "key": "mock/recording.wav",
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
