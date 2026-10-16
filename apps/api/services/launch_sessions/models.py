"""Request bodies of the launch-session endpoints (routers/launch_sessions.py)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class LaunchOptions(BaseModel):
    """What runLaunchPair computes in the browser today."""
    model_config = ConfigDict(extra="ignore")

    notify_recruiters: bool = True
    send_job_posting_email: Optional[bool] = None
    is_initial_launch: bool = False
    # Kept with the session's input for the record only: the browser already
    # folds the boards into send_job_posting_email (boards > 0), and no launch
    # path takes board ids (LaunchRequest has none).
    job_board_ids: List[Any] = Field(default_factory=list)
    batch_size: Optional[int] = Field(default=None, ge=1, le=200)
    # Recruiter warm handoff (see routers.engagement.SendBulkInterviewRequest).
    recruiter_handoff: bool = False
    handoff_timeout_sec: Optional[int] = Field(default=None, ge=60, le=1800)


class SearchAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = ""
    # Exactly what the browser would POST to /candidates/search for this attempt.
    payload: Dict[str, Any]


class CreateLaunchSessionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    idempotency_key: str = Field(min_length=1, max_length=200)
    mode: str = Field(pattern="^(full_search|provided)$")
    trigger: str = "button"
    search_attempts: List[SearchAttempt] = Field(default_factory=list, max_length=8)
    min_score: Optional[float] = None
    qualified_target: Optional[int] = Field(default=None, ge=1, le=1000)
    launch_count: Optional[int] = Field(default=None, ge=1, le=1000)
    # None = the server default (true on QA: PAIR_LAUNCH_REQUIRE_REVIEW_DEFAULT).
    require_review: Optional[bool] = None
    options: LaunchOptions = Field(default_factory=LaunchOptions)
    app_base_url: str = ""
    # provided mode: an optional first chunk of rows (≤ 50).
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    # provided mode: start right away (else status 'collecting' until /start).
    start: bool = False
    # "Launch remaining" after a server session: copy these rows from it
    # server-side (no re-upload). Implies provided mode and start.
    from_session_id: Optional[str] = None
    candidate_ids: List[str] = Field(default_factory=list)


class AddCandidatesRequest(BaseModel):
    candidates: List[Dict[str, Any]]


class ContactIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    phone: Optional[str] = None
    email: Optional[str] = None


class ConfirmReviewRequest(BaseModel):
    # In review mode the confirmed contact replaces BOTH channels: an omitted
    # or empty field means none — never a fallback to the row's own.
    contacts: Dict[str, ContactIn] = Field(default_factory=dict)
    # The rows to launch (default: every key of ``contacts``); selected rows
    # not listed are skipped ('review_removed').
    candidate_ids: Optional[List[str]] = None


class NeedsContactRequest(BaseModel):
    contacts: Dict[str, ContactIn] = Field(default_factory=dict)
    candidate_ids: List[str] = Field(default_factory=list)
    skip_ids: List[str] = Field(default_factory=list)
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


class RetryFailedRequest(BaseModel):
    candidate_ids: Optional[List[str]] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


class CancelRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=200)
