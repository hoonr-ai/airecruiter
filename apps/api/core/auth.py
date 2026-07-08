import os
import json
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from fastapi import Request, HTTPException, Depends, APIRouter, Header

try:
    import jwt
    from jwt import PyJWKClient
except ImportError:
    jwt = None
    PyJWKClient = None

logger = logging.getLogger(__name__)

_jwks_clients: Dict[str, Any] = {}


def get_jwks_client(tenant_id: str = "common") -> Any:
    if PyJWKClient is None:
        raise RuntimeError("PyJWT is not installed. Install PyJWT to verify Azure tokens.")
    if tenant_id not in _jwks_clients:
        jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        _jwks_clients[tenant_id] = PyJWKClient(jwks_url, cache_keys=True)
    return _jwks_clients[tenant_id]


def verify_azure_token(token: str) -> Optional[str]:
    """
    Verify an MSAL/Azure AD JWT token (ID token or access token) server-side
    using Azure's JWKS discovery endpoint.
    Returns the user's verified email address if valid, or None if invalid.
    """
    if not token or jwt is None:
        return None
    try:
        tenant_id = os.getenv("AZURE_TENANT_ID", "common").strip() or "common"
        jwks_client = get_jwks_client(tenant_id)
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
        options = {
            "verify_signature": True,
            "verify_exp": True,
            "verify_aud": bool(client_id),
            "verify_iss": False,
        }

        decode_kwargs = {
            "key": signing_key.key,
            "algorithms": ["RS256"],
            "options": options,
        }
        if client_id:
            decode_kwargs["audience"] = [client_id, f"api://{client_id}"]

        payload = jwt.decode(token, **decode_kwargs)
        email = (
            payload.get("preferred_username")
            or payload.get("upn")
            or payload.get("email")
            or payload.get("unique_name")
            or ""
        )
        if email:
            return str(email).strip().lower()
    except Exception as e:
        logger.warning("Azure token verification failed: %s", e)
    return None

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
    FastAPI dependency to extract the current authenticated user's identity.
    Validates server-side MSAL/Azure access token (Authorization: Bearer <token>) first.
    Falls back to trusted proxy headers or local dev email only if explicitly enabled.
    """
    email = ""

    # 1. Server-side token validation: check Authorization Bearer token
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth_header.strip().lower().startswith("bearer "):
        token = auth_header.strip()[7:].strip()
        email = verify_azure_token(token) or ""
        if not email:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired Bearer authentication token."
            )

    # 2. Check X-User-Email only if explicitly trusted via config/environment
    # (e.g. injected by authenticating reverse proxy like nginx, or in explicit dev mode)
    if not email:
        client_email = x_user_email if isinstance(x_user_email, str) else None
        client_email = client_email or request.headers.get("x-user-email") or request.headers.get("X-User-Email") or ""
        client_email = client_email.strip().lower()
        if client_email:
            trust_proxy_header = os.getenv("TRUST_X_USER_EMAIL", "false").lower() in ("1", "true", "yes")
            dev_mode = os.getenv("DEV_MODE", "false").lower() in ("1", "true", "yes")
            if trust_proxy_header or dev_mode:
                email = client_email
            else:
                logger.warning(
                    "Untrusted X-User-Email header ignored: %s (set TRUST_X_USER_EMAIL=true or provide Bearer token)",
                    client_email
                )

    # 3. Fallback for local dev environments where DEV_USER_EMAIL is explicitly configured
    if not email:
        email = os.getenv("DEV_USER_EMAIL", "").strip().lower()

    # 4. Fail closed by default unless DEV_ALLOW_UNAUTHENTICATED_ADMIN is explicitly enabled
    if not email:
        allow_unauth_admin = os.getenv("DEV_ALLOW_UNAUTHENTICATED_ADMIN", "false").lower() in ("1", "true", "yes")
        if allow_unauth_admin:
            return UserIdentity(email="unauthenticated@hoonr.ai", role="admin")
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide a valid Authorization Bearer token."
        )

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
