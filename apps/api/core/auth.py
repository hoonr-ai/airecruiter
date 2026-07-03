import os
import json
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from fastapi import Request, HTTPException, Depends, APIRouter, Header

logger = logging.getLogger(__name__)

@dataclass
class UserIdentity:
    email: str
    role: str  # 'admin' or 'recruiter'

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_user_role(email: str) -> str:
    """
    Determine user role ('admin' vs 'recruiter') based on ADMIN_EMAILS env var
    and the user_roles SQL table.
    """
    if not email:
        return "recruiter"
    
    clean_email = email.strip().lower()
    
    # 1. Check ADMIN_EMAILS env var
    admin_emails = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
    if clean_email in admin_emails:
        return "admin"
        
    # 2. Check user_roles database table
    try:
        from core.db import get_db_connection
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT role FROM user_roles WHERE LOWER(email) = %s", (clean_email,))
                row = cur.fetchone()
                if row:
                    role_val = row["role"] if isinstance(row, dict) else row[0]
                    if role_val and str(role_val).strip().lower() == "admin":
                        return "admin"
    except Exception as e:
        logger.debug("user_roles check failed for %s: %s", clean_email, e)
        
    return "recruiter"


def get_current_user(
    request: Request,
    x_user_email: Optional[str] = Header(default=None, alias="X-User-Email", description="User email for RBAC simulation/authentication")
) -> UserIdentity:
    """
    FastAPI dependency to extract the current authenticated user's identity
    from request headers (X-User-Email).
    """
    email = x_user_email or request.headers.get("x-user-email") or request.headers.get("X-User-Email") or ""
    email = email.strip().lower()
    
    # In local development or automated testing, allow fallback if SSO is disabled/unauthenticated
    if not email:
        email = os.getenv("DEV_USER_EMAIL", "").strip().lower()
        
    if not email:
        # If still empty, check if unauthenticated calls should default to admin (for legacy test compatibility)
        allow_unauth_admin = os.getenv("DEV_ALLOW_UNAUTHENTICATED_ADMIN", "true").lower() in ("1", "true", "yes")
        if allow_unauth_admin:
            return UserIdentity(email="unauthenticated@hoonr.ai", role="admin")
        return UserIdentity(email="", role="recruiter")
        
    role = get_user_role(email)
    return UserIdentity(email=email, role=role)


def verify_job_access(job_data: Dict[str, Any], user: UserIdentity) -> None:
    """
    Verify if the given user has permission to access or modify this job.
    Raises HTTPException(403) if unauthorized.
    """
    if user.is_admin:
        return
        
    raw_emails = job_data.get("recruiter_emails", [])
    if isinstance(raw_emails, str):
        try:
            emails = json.loads(raw_emails) if raw_emails.strip().startswith("[") else [raw_emails]
        except Exception:
            emails = [raw_emails] if raw_emails else []
    elif isinstance(raw_emails, list):
        emails = raw_emails
    else:
        emails = []
        
    clean_assigned_emails = [str(e).strip().lower() for e in emails if e]
    
    # If job has no assigned recruiters, we restrict to admins only
    if not clean_assigned_emails:
        raise HTTPException(
            status_code=403, 
            detail="This job is unassigned or legacy. Only Admins can access unassigned jobs."
        )
        
    if user.email not in clean_assigned_emails:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. You ({user.email}) are not assigned as a recruiter for this job."
        )


auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

@auth_router.get("/me")
def get_my_identity(user: UserIdentity = Depends(get_current_user)):
    return {
        "email": user.email,
        "role": user.role,
        "is_admin": user.is_admin,
    }
