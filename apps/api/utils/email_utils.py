import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PLACEHOLDER_EMAILS = {
    "your-email@example.com",
    "email@example.com",
    "example@example.com",
    "test@example.com",
    "candidate@example.com",
    "noreply@example.com",
}

_PLACEHOLDER_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "invalid",
    "localhost",
    "local",
}

def is_placeholder_email(email: str) -> bool:
    """Check if an email is synthetic, dummy, or placeholder."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return True
    if normalized in _PLACEHOLDER_EMAILS:
        return True
    if normalized.endswith("@noemail.pair.ai"):
        return True
    if "@" not in normalized:
        return True
    local_part, domain = normalized.rsplit("@", 1)
    if domain in _PLACEHOLDER_DOMAINS:
        return True
    # Catch subdomains of .local (e.g. no-email.jobdiva.local, jobdiva.local)
    if domain.endswith(".local") or domain == "local":
        return True
    if local_part in {"your-email", "your_email", "email", "test", "example", "candidate"}:
        return True
    # JobDiva auto-generates "Auto_<candidateId>@jobdiva.com" when a candidate
    # has no real email on file — these are dead addresses, not contactable.
    if domain == "jobdiva.com":
        return True
    return False
