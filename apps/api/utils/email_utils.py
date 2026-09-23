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

_FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "mail.com",
    "protonmail.com",
    "yandex.com",
    "zoho.com",
    "gmx.com",
    "live.com",
    "msn.com",
    "me.com",
    "mac.com",
    "proton.me",
    "gmx.net",
    "fastmail.com",
    "qq.com",
    "163.com",
    "rediffmail.com",
    "ymail.com",
    "yahoo.co.uk",
    "hotmail.co.uk",
    "live.co.uk",
    "googlemail.com",
}

def is_work_email(email: str) -> bool:
    """Determine if an email is likely a work email based on its domain.
    Returns True if the domain is NOT in the list of known free email providers.
    """
    if not email or "@" not in email:
        return False
    
    normalized = email.strip().lower()
    _, domain = normalized.rsplit("@", 1)
    
    # If it's a known placeholder, we don't classify it as a valid work email either
    if is_placeholder_email(normalized):
        return False
        
    return domain not in _FREE_EMAIL_DOMAINS

