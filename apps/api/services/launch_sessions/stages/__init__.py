"""The launch-session stage machine: one module per stage.

    created ─▶ sourcing (S1, full_search) ─▶ selecting (S2) ─▶ enriching (S3)
            └──────────── provided ─────────┘                      │
       ┌──────────────────────── require_review ◀─────────────────┤
       ▼                                                            ▼
    review (S4, awaiting_input) ─▶ splitting (S5) ─▶ saving (S6) ─▶ launching (S7)
                                                                    │
                                    done (S8) ◀── provisioning (S7b) ◀┘

Each stage is ``async def run(ctx) -> StageResult``: idempotent given the
session row and its checkpoint (re-running a stage after a crash after ANY
unit resumes, never repeats a side effect), every write fenced through ctx.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Dict

from services.launch_sessions import schema as S
from services.launch_sessions.context import StageContext, StageResult
from services.launch_sessions.stages import (
    enriching,
    finishing,
    launching,
    provisioning,
    review,
    saving,
    selecting,
    sourcing,
    splitting,
)

StageFn = Callable[[StageContext], Awaitable[StageResult]]

STAGE_FUNCTIONS: Dict[str, StageFn] = {
    S.STAGE_SOURCING: sourcing.run,
    S.STAGE_SELECTING: selecting.run,
    S.STAGE_ENRICHING: enriching.run,
    S.STAGE_REVIEW: review.run,
    S.STAGE_SPLITTING: splitting.run,
    S.STAGE_SAVING: saving.run,
    S.STAGE_LAUNCHING: launching.run,
    S.STAGE_PROVISIONING: provisioning.run,
    S.STAGE_DONE: finishing.run,
}


def first_stage(session: dict) -> str:
    """created → sourcing for a full search, selecting for provided rows."""
    return S.STAGE_SOURCING if session.get("mode") == S.MODE_FULL_SEARCH else S.STAGE_SELECTING
