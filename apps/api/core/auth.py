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
    Verify an MSAL/Azure AD JWT token (ID token or access token) server-side.
    Attempts JWKS signature verification first; gracefully handles Microsoft Graph
    access tokens (which use internal Microsoft signing keys) by verifying
    expiration, issuer, and audience.
    Returns the user's verified email address if valid, or None if invalid.
    """
    if not token or jwt is None:
        return None

    try:
        unverified_payload = jwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        logger.warning("Failed to decode token structure: %s", e)
        return None

    import time
    exp = unverified_payload.get("exp")
    if exp and time.time() > float(exp):
        logger.warning("Azure token expired at %s", exp)
        return None

    client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
    valid_audiences = [
        client_id,
        f"api://{client_id}",
        "00000003-0000-0000-c000-000000000000",
        "https://graph.microsoft.com",
    ]
    aud = unverified_payload.get("aud")
    if client_id and aud:
        aud_list = [aud] if isinstance(aud, str) else aud
        if not any(a in valid_audiences for a in aud_list):
            logger.warning("Token audience %s not in allowed list", aud)
            return None

    iss = str(unverified_payload.get("iss", "")).lower()
    if iss and not (
        "login.microsoftonline.com" in iss
        or "sts.windows.net" in iss
        or "login.windows.net" in iss
    ):
        logger.warning("Untrusted token issuer: %s", iss)
        return None

    email = (
        unverified_payload.get("preferred_username")
        or unverified_payload.get("upn")
        or unverified_payload.get("email")
        or unverified_payload.get("unique_name")
        or ""
    )
    if not email:
        return None

    # Cryptographically verify the token signature against Microsoft's JWKS.
    # This is the ONLY step that binds the token to a real Azure-issued identity —
    # every claim read above came from the UNVERIFIED payload. If verification
    # fails for any reason we MUST fail closed (reject); never trust the
    # unverified email, otherwise a self-signed/forged JWT with a spoofed
    # `preferred_username` (e.g. an admin address) would authenticate as that user.
    try:
        tid = unverified_payload.get("tid") or "common"
        jwks_client = get_jwks_client(str(tid))
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=valid_audiences if client_id else None,
            options={"verify_signature": True, "verify_exp": True, "verify_aud": bool(client_id)},
        )
    except Exception as jwks_err:
        logger.warning(
            "Rejecting token: JWKS signature verification failed (%s): %s",
            type(jwks_err).__name__, jwks_err,
        )
        return None

    if not client_id:
        # Signature is verified, but without AZURE_CLIENT_ID we cannot bind the
        # token to THIS application: any RS256 token Microsoft signed (any app /
        # any tenant on the 'common' JWKS) would pass. Set AZURE_CLIENT_ID in
        # every deployed environment so the audience/tenant is enforced.
        logger.warning(
            "AZURE_CLIENT_ID is not set: token audience is NOT enforced. "
            "Set AZURE_CLIENT_ID to bind tokens to this application."
        )

    return str(email).strip().lower()

@dataclass
class UserIdentity:
    email: str
    # 'admin' | 'team_lead' | 'recruiter'. 'team_lead' is the one non-admin role
    # with a view wider than the caller's own jobs: it covers a lead of a
    # Teams-page team AND anyone who manages people in the org hierarchy
    # (Resource Manager and above), so every page gated on it admits both.
    role: str
    # Populated when the user belongs to a team (lead or member). Admins keep
    # role='admin' even when they lead a team — the full admin view wins.
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    # 'lead' | 'member' when team_id is set. Needed because role='team_lead' no
    # longer implies leading THIS team: a hierarchy manager can be a plain member.
    team_member_role: Optional[str] = None
    # Org hierarchy node (services/org_hierarchy.py); None when not in the tree.
    org_member_id: Optional[int] = None
    org_role: Optional[str] = None
    org_manages_people: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_team_lead(self) -> bool:
        return self.role == "team_lead"

    @property
    def leads_team(self) -> bool:
        """Leads a Teams-page team (not merely belongs to one).

        When the member role is unknown (an identity built by hand), the old rule
        applies: a team_lead with a team leads it.
        """
        if not self.team_id:
            return False
        if self.team_member_role is not None:
            return self.team_member_role == "lead"
        return self.role == "team_lead"

    @property
    def manages_org(self) -> bool:
        """Has at least one person beneath them in the org hierarchy."""
        return self.org_member_id is not None and self.org_manages_people


def get_user_role(email: str) -> str:
    """
    Determine user role ('admin' vs 'recruiter') based on ADMIN_EMAILS env var
    and the user_roles SQL table. Team-lead promotion happens on top of this
    in resolve_user_identity (admin always wins over team_lead).
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


def resolve_user_identity(email: str) -> UserIdentity:
    """Full identity resolution: base role + team membership + org hierarchy.

    A non-admin who is a 'lead' on a team, or who has people beneath them in
    the org hierarchy, resolves to role='team_lead'. Team id/name are attached
    for any team member so scoped endpoints can reuse them without re-querying.

    Both lookups are best-effort and fail CLOSED: if one errors, the user simply
    gets no extra visibility from it (never more).
    """
    role = get_user_role(email)
    team_id = None
    team_name = None
    team_member_role = None
    try:
        # Late import: services.teams_db pulls core.db; importing it lazily
        # keeps core.auth import-safe during early app boot and tests.
        from services import teams_db

        membership = teams_db.get_team_for_email(email)
        if membership:
            team_id = membership.get("team_id")
            team_name = membership.get("team_name")
            team_member_role = membership.get("member_role")
            if role != "admin" and team_member_role == "lead":
                role = "team_lead"
    except Exception as e:
        logger.debug("team membership check failed for %s: %s", email, e)

    org_member_id = None
    org_role = None
    org_manages_people = False
    try:
        from services import org_hierarchy

        org = org_hierarchy.get_membership(email)
        if org:
            org_member_id = org["id"]
            org_role = org["role"]
            org_manages_people = bool(org["has_reports"])
            if role != "admin" and org_manages_people:
                role = "team_lead"
    except Exception as e:
        logger.debug("org hierarchy check failed for %s: %s", email, e)

    return UserIdentity(
        email=email,
        role=role,
        team_id=team_id,
        team_name=team_name,
        team_member_role=team_member_role,
        org_member_id=org_member_id,
        org_role=org_role,
        org_manages_people=org_manages_people,
    )


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
    # AND the environment is explicitly local/test (impossible to leave on in prod)
    if not email:
        env = os.getenv("ENV", "").strip().lower() or os.getenv("ENVIRONMENT", "").strip().lower()
        is_local_env = env in ("local", "test", "dev", "development")
        allow_unauth_admin = os.getenv("DEV_ALLOW_UNAUTHENTICATED_ADMIN", "false").lower() in ("1", "true", "yes")
        if allow_unauth_admin and is_local_env:
            logger.warning("Unauthenticated admin fallback triggered in '%s' environment.", env)
            return UserIdentity(email="unauthenticated@hoonr.ai", role="admin")
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide a valid Authorization Bearer token."
        )

    return resolve_user_identity(email)


def get_user_scope_emails(user: UserIdentity) -> set:
    """Emails whose job assignments this user may see.

    Recruiters: themselves. Team leads: themselves + everyone on their team.
    Org-hierarchy managers: themselves + everyone beneath them at any depth.
    Someone who is both gets the union, so adding the hierarchy never removes
    access. (Admins bypass scope checks entirely — callers check is_admin first.)

    This is the ONE definition of "who can this user see": the jobs list,
    verify_job_access and the admin reports all derive from it (the reports
    through report_scope_parts, which names the same two sources), so a
    manager's jobs and analytics can never describe different people.
    """
    allowed = {user.email}
    if user.leads_team:
        try:
            from services import teams_db

            allowed.update(teams_db.get_team_member_emails(user.team_id))
        except Exception as e:
            logger.debug("team scope emails lookup failed for %s: %s", user.email, e)
    if user.manages_org:
        try:
            from services import org_hierarchy

            allowed.update(org_hierarchy.scope_emails(user.org_member_id))
        except Exception as e:
            logger.debug("org scope emails lookup failed for %s: %s", user.email, e)
    return allowed


def report_scope_parts(user: UserIdentity) -> List[str]:
    """The scope parts behind get_user_scope_emails, as report scope keys.

    `org:<id>` = the user's organisation in the hierarchy; a bare id = the
    Teams-page team they lead. Order is stable so equal scopes share a cache key.
    """
    parts: List[str] = []
    if user.manages_org:
        parts.append(f"org:{user.org_member_id}")
    if user.leads_team:
        parts.append(str(user.team_id))
    return parts


def resolve_report_scope(user: UserIdentity, requested: Optional[str], what: str) -> Optional[str]:
    """Scope key for an admin report: who the numbers are about.

    - Admins: whatever they ask for (a Teams-page team id, an `org:<id>` key, or
      nothing = everyone).
    - Team leads / org managers: always their own scope; a requested scope is
      ignored, never honoured.
    - Everyone else: 403.

    `what` completes the 403 message ("... to view <what>."). The result is the
    `scope_team_id` the reports pass to routers._helpers._load_team_scope.
    """
    if user.is_admin:
        return (requested or "").strip() or None
    parts = report_scope_parts(user)
    if user.is_team_lead and parts:
        return "+".join(parts)
    raise HTTPException(
        status_code=403,
        detail=f"Access denied. Admin or team lead access required to view {what}.",
    )


def parse_recruiter_emails(raw: Any) -> List[str]:
    """Normalize a `recruiter_emails` value — stored as a JSON string, a bare
    string, or a native list depending on the read path — into a clean,
    lowercased list. Shared by every caller that decides access/visibility/
    launch eligibility off this field, so they can't drift (verify_job_access,
    routers/jobs.py's _filter_jobs_for_user, routers/candidates.py's launch
    gate)."""
    if isinstance(raw, str):
        try:
            emails = json.loads(raw) if raw.strip().startswith("[") else [raw]
        except Exception:
            emails = [raw] if raw else []
    elif isinstance(raw, list):
        emails = raw
    else:
        emails = []
    return [str(e).strip().lower() for e in emails if e and str(e).strip()]


def verify_job_access(job_data: Dict[str, Any], user: UserIdentity) -> None:
    """
    Verify if the given user has permission to access or modify this job.
    Raises HTTPException(403) if unauthorized.
    """
    if user.is_admin:
        return

    clean_assigned_emails = parse_recruiter_emails(job_data.get("recruiter_emails", []))

    # If job has no assigned recruiters (legacy or unassigned), allow authenticated recruiters to access/claim it
    if not clean_assigned_emails:
        return

    # Team leads can access any job assigned to someone on their team.
    if set(clean_assigned_emails).isdisjoint(get_user_scope_emails(user)):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. You ({user.email}) are not assigned as a recruiter for this job."
        )


auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

@auth_router.get("/me")
def get_my_identity(user: UserIdentity = Depends(get_current_user)):
    org_role = user.org_role if user.org_member_id is not None else None
    org_role_label = None
    if org_role:
        from services import org_hierarchy

        org_role_label = org_hierarchy.role_label(org_role)
    return {
        "email": user.email,
        "role": user.role,
        "is_admin": user.is_admin,
        "is_team_lead": user.is_team_lead,
        "team_id": user.team_id,
        "team_name": user.team_name,
        # Org hierarchy: the person's level (for labels) and whether anyone
        # reports to them. Visibility itself is enforced server-side.
        "org_role": org_role,
        "org_role_label": org_role_label,
        "manages_people": user.manages_org,
    }
