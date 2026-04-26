from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx


AMPLITUDE_API_URL = os.getenv("AMPLITUDE_API_URL", "https://api2.amplitude.com/2/httpapi")
AMPLITUDE_API_KEY = os.getenv("AMPLITUDE_API_KEY", "")


def amplitude_enabled() -> bool:
    return bool(AMPLITUDE_API_KEY)


def _sanitize_properties(props: dict[str, Any] | None) -> dict[str, Any]:
    if not props:
        return {}
    clean: dict[str, Any] = {}
    for key, value in props.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
            continue
        clean[key] = str(value)
    return clean


async def _send_event(
    event_type: str,
    event_properties: dict[str, Any] | None = None,
    user_id: str | None = None,
    device_id: str | None = None,
) -> None:
    if not amplitude_enabled():
        return

    event_payload = {
        "event_type": event_type,
        "event_properties": _sanitize_properties(event_properties),
    }
    if user_id:
        event_payload["user_id"] = user_id
    elif device_id:
        event_payload["device_id"] = device_id
    else:
        event_payload["device_id"] = "airecruiter-api"

    payload = {
        "api_key": AMPLITUDE_API_KEY,
        "events": [event_payload],
    }

    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            await client.post(AMPLITUDE_API_URL, json=payload)
    except Exception:
        # Never break app behavior because telemetry failed.
        return


def track_event_async(
    event_type: str,
    event_properties: dict[str, Any] | None = None,
    user_id: str | None = None,
    device_id: str | None = None,
) -> None:
    if not amplitude_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_event(event_type, event_properties, user_id, device_id))
    except RuntimeError:
        # No running loop (e.g. sync logger path). Skip silently.
        return
