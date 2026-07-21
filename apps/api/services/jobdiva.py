import asyncio
import logging
import re
import time
import json
import httpx as _httpx_module
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

from utils.phone import normalize_phone
from html import unescape
import sqlalchemy
from sqlalchemy import text
from core import (
    JOBDIVA_API_URL, JOBDIVA_CLIENT_ID, JOBDIVA_USERNAME, 
    JOBDIVA_PASSWORD, DATABASE_URL, DEBUG_LOG_PATH,
    JOBDIVA_PAIR_RECRUITER_ID
)

logger = logging.getLogger(__name__)


# --- JobDiva HTTP request/response logging to New Relic ------------------
# Every httpx.AsyncClient(...) constructed in this module is transparently
# wrapped (via the _JDHttpxProxy shim below) so each JobDiva request emits
# a New Relic custom event + message with the full response body and
# elapsed time. We do not modify the global httpx module — only the local
# `httpx` name in this file's namespace.

_JD_RESPONSE_BODY_LIMIT = 16000  # cap body bytes sent to New Relic per call


def _jd_redact_url(url: "_httpx_module.URL") -> str:
    s = str(url)
    if "password=" in s:
        s = re.sub(r"(password=)[^&]*", r"\1***", s)
    return s


async def _jd_on_request(request: "_httpx_module.Request") -> None:
    request.extensions["_jd_t0"] = time.monotonic()


async def _jd_on_response(response: "_httpx_module.Response") -> None:
    try:
        t0 = response.request.extensions.get("_jd_t0")
        elapsed_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None

        # Buffer the body so the caller's .text/.json() still works after us.
        body_text = ""
        try:
            await response.aread()
            body_text = response.text or ""
        except Exception:
            body_text = ""
        body_size = len(body_text)
        body_truncated = body_text[:_JD_RESPONSE_BODY_LIMIT]
        was_truncated = body_size > len(body_truncated)

        try:
            from core.newrelic import is_enabled, record_custom_event, record_message
        except Exception:
            return
        if not is_enabled():
            return

        method = response.request.method
        url_path = response.request.url.path
        url_full = _jd_redact_url(response.request.url)
        status = response.status_code

        try:
            event_data = {
                "url": url_full,
                "endpoint": url_path,
                "method": method,
                "status_code": status,
                "elapsed_ms": elapsed_ms,
                "response_size_bytes": body_size,
                "truncated": was_truncated,
                "response_body": body_truncated,
            }
            record_custom_event("JobDivaAPI", event_data)
            level = "info" if 200 <= status < 400 else ("warning" if status < 500 else "error")
            record_message(
                f"JobDiva {method} {url_path} -> {status} ({elapsed_ms}ms)",
                attributes=event_data,
                level=level,
            )
        except Exception:
            pass
    except Exception:
        # Logging must never break the actual API path.
        return


class _JDAsyncClient(_httpx_module.AsyncClient):
    def __init__(self, *args, **kwargs):
        hooks = kwargs.pop("event_hooks", None) or {}
        request_hooks = list(hooks.get("request", [])) + [_jd_on_request]
        response_hooks = list(hooks.get("response", [])) + [_jd_on_response]
        kwargs["event_hooks"] = {"request": request_hooks, "response": response_hooks}
        super().__init__(*args, **kwargs)


class _JDHttpxProxy:
    """Module-local stand-in for the `httpx` module.

    AsyncClient is overridden to inject New Relic logging hooks; every other
    attribute (TimeoutException, ConnectError, Request, ...) falls through
    to the real httpx module untouched.
    """
    AsyncClient = _JDAsyncClient

    def __getattr__(self, name):
        return getattr(_httpx_module, name)


httpx = _JDHttpxProxy()


_CANDIDATE_EMAIL_KEYS = [
    "email",
    "EMAIL",
    "emailAddress",
    "EMAILADDRESS",
    "emails",
    "EMAILS",
    "emailId",
    "EMAILID",
    "email1",
    "EMAIL1",
    "email2",
    "EMAIL2",
    "alternateEmail",
    "ALTERNATEEMAIL",
]

_CANDIDATE_PHONE_KEYS = [
    "phone",
    "PHONE",
    "phoneNumber",
    "PHONENUMBER",
    "mobilePhone",
    "MOBILEPHONE",
    "cellPhone",
    "CELLPHONE",
    "homePhone",
    "HOMEPHONE",
    "workPhone",
    "WORKPHONE",
    "phone1",
    "PHONE1",
    "phone2",
    "PHONE2",
    "phone3",
    "PHONE3",
    "primaryPhone",
    "PRIMARYPHONE",
]

# LLM-only candidate enrichment is active for sourcing.

# TEMPORARY DEBUG LOGGER
def debug_log(msg):
    if not DEBUG_LOG_PATH:
        return
    try:
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(f"[{time.ctime()}] {msg}\n")
    except:
        pass

logger = logging.getLogger(__name__)

def readable_ist_now() -> str:
    """Returns current IST time in readable format: 2026-02-24 16:25:59 IST"""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")


# JobDiva occasionally returns 2FA / dialog UI text inside firstName/lastName
# fields (observed: "Confirm Verification" / "Code" across 23+ records in a
# single response). These pollute the candidate table with non-person rows.
# This validator rejects names that look like UI strings, not human names.
_NAME_POLLUTION_TOKENS = (
    "verification", "captcha", "confirm", "submit", "continue",
    "please click", "sign in", "log in", "login", "enter code",
)
_NAME_POLLUTION_EXACT = {
    "confirm verification", "code", "ok", "cancel", "yes", "no",
}


def is_valid_candidate_name(first: str, last: str) -> bool:
    """Return False for obvious JobDiva UI-string pollution disguised as a name.

    Conservative on purpose — only triggers on clearly non-person values. A real
    person named "Code" (unlikely but possible) would currently get rejected
    via the EXACT set; that risk is preferred over admitting another batch of
    "Confirm Verification Code" rows.
    """
    f = (first or "").strip()
    l = (last or "").strip()
    if not f and not l:
        return False
    combined = f"{f} {l}".strip().lower()
    if combined in _NAME_POLLUTION_EXACT or f.lower() in _NAME_POLLUTION_EXACT or l.lower() in _NAME_POLLUTION_EXACT:
        return False
    if any(tok in combined for tok in _NAME_POLLUTION_TOKENS):
        return False
    if len(f) > 30 or len(l) > 30:
        return False
    return True

def get_field(data: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    """
    Safely extract a value from a dictionary by checking multiple potential keys
    case-insensitively and ignoring non-alphanumeric characters.
    Enhanced to filter out unwanted values like "Direct Placement" from location fields.
    """
    if not isinstance(data, dict):
        return default
      
    def normalize(s):
        return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())
      
    normalized_data = {normalize(k): v for k, v in data.items()}
  
    for key in keys:
        norm_key = normalize(key)
        if norm_key in normalized_data:
            val = normalized_data[norm_key]
            # JobDiva returns many slot-style fields (PHONE1..PHONE4, EMAIL1/EMAIL2,
            # ADDRESS1/ADDRESS2) where the earlier slots are blank for a given
            # candidate but a later slot has the real value. Treat blank scalars
            # as "field absent" so the loop falls through to the next candidate
            # key instead of returning "" and shadowing the real value.
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            # Handle JobDiva returning lists for fields like email or phone when a candidate has multiple
            if isinstance(val, list) and val:
                # JobDiva lists might be strings or dicts
                first_valid = None
                for item in val:
                    if isinstance(item, dict):
                        for subkey in ["dateTime", "date", "value", "$"]:
                            if subkey in item:
                                item = item[subkey]
                                break
                    if isinstance(item, str) and item.strip():
                        first_valid = item.strip()
                        break
                val = first_valid if first_valid is not None else str(val[0])
                
            # Handle JobDiva's nested date/time objects
            if isinstance(val, dict):
                for subkey in ["dateTime", "date", "value", "$"]:
                    if subkey in val:
                        val = val[subkey]
                        break
            
            
            # Filter out employment-related values from location fields
            if isinstance(val, str) and _is_location_key(key):
                val_lower = val.lower().strip()
                # Don't return employment types as location data
                employment_indicators = [
                    "direct placement", "contract", "full-time", "part-time", 
                    "w2", "1099", "c2c", "corp to corp", "open", "pending",
                    "temporary", "permanent", "temp to perm", "fulltime", "parttime",
                    "consultant", "consulting", "employee", "contractor"
                ]
                if any(indicator in val_lower for indicator in employment_indicators):
                    continue
            
            return val
          
    return default

def _is_location_key(key: str) -> bool:
    """Check if a key represents a location-related field"""
    location_keywords = [
        "city", "state", "zip", "location", "address", "province", 
        "postal", "worksite", "jobcity", "jobstate", "locationcity", 
        "locationstate", "worksitecity", "worksitestate"
    ]
    key_lower = key.lower()
    return any(keyword in key_lower for keyword in location_keywords)

def _clean_location_field(value: Any) -> str:
    """Clean location field values to remove employment type contamination"""
    if not value:
        return ""
    
    val_str = str(value).strip()
    if not val_str:
        return ""
    
    val_lower = val_str.lower()
    
    # Don't return employment-related values as location
    employment_indicators = [
        "direct placement", "contract", "full-time", "part-time", 
        "w2", "1099", "c2c", "corp to corp", "open", "pending",
        "temporary", "permanent", "temp to perm", "fulltime", "parttime",
        "consultant", "consulting", "employee", "contractor"
    ]
    
    if any(indicator in val_lower for indicator in employment_indicators):
        return ""
    
    return val_str


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
_PLACEHOLDER_LOCALPARTS = {"your-email", "your_email", "email", "test", "example", "candidate"}


def _is_placeholder_email(email: str) -> bool:
    """Mirror of routers.engagement._is_placeholder_email so JobDiva extraction
    skips synthetic/placeholder emails before they propagate downstream."""
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
    if local_part in _PLACEHOLDER_LOCALPARTS:
        return True
    # JobDiva auto-generates "Auto_<candidateId>@jobdiva.com" when a candidate
    # has no real email on file. Treat any @jobdiva.com address as synthetic so
    # a real ALTERNATEEMAIL is preferred and PAIR never emails a dead address.
    if domain == "jobdiva.com":
        return True
    return False


def _collect_field_values(data: Dict[str, Any], keys: List[str]) -> List[str]:
    """Flatten every value found across `keys` (case/punctuation-insensitive),
    expanding list and nested-date/value shapes into individual scalar strings.

    Unlike get_field (which collapses to the first match), this preserves every
    candidate value so callers like email/phone selection can apply their own
    preference rule (e.g. prefer a non-placeholder email over the first one).
    """
    if not isinstance(data, dict):
        return []

    def normalize(s):
        return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())

    normalized_data = {normalize(k): v for k, v in data.items()}

    def unwrap(item: Any) -> Optional[str]:
        if isinstance(item, dict):
            for subkey in ["dateTime", "date", "value", "$"]:
                if subkey in item:
                    item = item[subkey]
                    break
        if item is None:
            return None
        s = str(item).strip()
        return s or None

    values: List[str] = []
    for key in keys:
        norm_key = normalize(key)
        if norm_key not in normalized_data:
            continue
        val = normalized_data[norm_key]
        items = val if isinstance(val, list) else [val]
        for item in items:
            s = unwrap(item)
            if s:
                values.append(s)
    return values


def _get_candidate_email(data: Dict[str, Any]) -> str:
    """Return the candidate's best email: the first well-formed, non-placeholder
    address across all email keys/list items. Falls back to the first well-formed
    address only when every candidate is a placeholder, and finally to the first
    raw value so behavior never regresses to empty when something is present."""
    values = _collect_field_values(data, _CANDIDATE_EMAIL_KEYS)
    if not values:
        return ""

    well_formed = [v for v in values if _EMAIL_RE.match(v.strip().lower())]
    for v in well_formed:
        if not _is_placeholder_email(v):
            return v.strip()
    if well_formed:
        return well_formed[0].strip()
    return values[0].strip()


def _candidate_phone_with_type(data: Dict[str, Any]) -> Tuple[str, bool]:
    """Return ``(best_phone, is_mobile)`` for a candidate / CandidatesDetail record.

    JobDiva returns numbers in slots (CELLPHONE, PHONE1..PHONE4) where each
    PHONE{n} carries a companion PHONE{n}_TYPE ('Mobile Phone', 'Home Phone',
    'Work Phone', 'Home Fax'). PAIR contacts candidates on their mobile, so a
    blank CELLPHONE must not let a Home/Work number in an earlier slot shadow a
    real mobile sitting in a later, type-tagged slot. Preference order:
      1) explicit mobile/cell fields (CELLPHONE, mobilePhone)  -> is_mobile=True
      2) any PHONE{n} whose PHONE{n}_TYPE says mobile/cell      -> is_mobile=True
      3) otherwise the first phone slot that actually contains  -> is_mobile=False
         digits.
    `is_mobile` lets callers do an upgrade-only merge (never downgrade a mobile
    to a home/work number). A value without digits is never a phone.
    """
    if not isinstance(data, dict):
        return "", False

    def normalize(s):
        return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())

    norm = {normalize(k): v for k, v in data.items()}

    def scalar(v: Any) -> str:
        if isinstance(v, list):
            v = next((x for x in v if x), None)
        if isinstance(v, dict):
            for sk in ["value", "$"]:
                if sk in v:
                    v = v[sk]
                    break
        return str(v).strip() if v is not None else ""

    def has_digits(s: str) -> bool:
        return any(ch.isdigit() for ch in s)

    # 1) Explicit mobile/cell fields.
    for key in ("mobilephone", "cellphone", "mobile", "cell"):
        s = scalar(norm.get(key))
        if has_digits(s):
            return s, True

    # 2) Slotted PHONE{n} whose companion PHONE{n}_TYPE indicates mobile/cell.
    for n in range(1, 5):
        s = scalar(norm.get(f"phone{n}"))
        t = scalar(norm.get(f"phone{n}type")).lower()
        if has_digits(s) and ("mobile" in t or "cell" in t):
            return s, True

    # 3) Fallback: first phone value that actually contains digits (non-mobile).
    for v in _collect_field_values(data, _CANDIDATE_PHONE_KEYS):
        if has_digits(v):
            return v.strip(), False
    return "", False


def _get_candidate_phone(data: Dict[str, Any]) -> str:
    """Return the candidate's best phone, preferring a MOBILE/cell number.

    Thin wrapper over :func:`_candidate_phone_with_type` (see it for the slot
    selection rules).
    """
    return _candidate_phone_with_type(data)[0]


def _select_better_phone(existing: Optional[str], detail: Dict[str, Any]) -> str:
    """Upgrade-only phone merge from a CandidatesDetail-style record.

    Never downgrades. The détail phone wins ONLY when the existing number is
    empty/invalid, OR the détail phone is a typed mobile/cell that differs from
    the existing one. A non-mobile détail phone never replaces a non-empty
    existing number, and an invalid détail phone never replaces a valid one.
    Returns the phone string to keep.
    """
    existing = (existing or "").strip()
    new_phone, new_is_mobile = _candidate_phone_with_type(detail)
    new_phone = (new_phone or "").strip()
    if not new_phone or normalize_phone(new_phone) is None:
        return existing  # never replace with an empty/invalid number
    if not existing or normalize_phone(existing) is None:
        return new_phone  # fill an empty / unusable existing number
    if new_is_mobile and normalize_phone(new_phone) != normalize_phone(existing):
        return new_phone  # upgrade to a (different) typed mobile
    return existing  # keep existing — no lateral move / downgrade


def _is_job_agent_criteria_unconfigured(status_code: int, body: str) -> bool:
    """Detect JobDiva 'criteria not configured' responses robustly.

    JobDiva has returned multiple variants over time (status and wording), e.g.
    "Criteria Not Assigned", "criteria not configured", JSON error wrappers,
    and mixed casing. Keep this tolerant so Step-5 pre-check can consistently
    trigger the recruiter guidance modal.
    """
    text = (body or "").lower()
    if not text:
        return False

    # Primary known phrase from JobAgentSearch.
    if "criteria not assigned" in text:
        return True

    # Tolerate wording variations commonly returned by gateways/wrappers.
    has_criteria = "criteria" in text
    has_negative = (
        "not assigned" in text
        or "not configured" in text
        or "not setup" in text
        or "not set up" in text
        or "missing" in text
    )
    # These are expected for this specific mismatch; keep strict enough to
    # avoid false positives on unrelated errors.
    is_error_status = int(status_code or 0) >= 400
    return bool(is_error_status and has_criteria and has_negative)

def format_job_description(raw_desc: str) -> str:
    """
    Format raw job description with minimal changes - keep exact text, just clean HTML.
    """
    if not raw_desc or not raw_desc.strip():
        return "No job description available."
  
    desc = unescape(raw_desc)
    desc = re.sub(r'<br\s*/?>', '\n', desc)
    desc = re.sub(r'<p>', '\n', desc)
    desc = re.sub(r'</p>', '\n', desc)
    desc = re.sub(r'<div[^>]*>', '\n', desc)
    desc = re.sub(r'</div>', '\n', desc)
    desc = re.sub(r'<[^>]*>', '', desc)
  
    desc = re.sub(r'\n\s*\n\s*\n+', '\n\n', desc)
    desc = re.sub(r'[ \t]+', ' ', desc)
    desc = desc.strip()
  
    return desc

def extract_pay_rate_from_text(description: str) -> str:
    """Extract pay rate from job description text as fallback when structured fields are missing"""
    if not description:
        return ""
    
    # Patterns to match various pay rate formats
    pay_patterns = [
        # "Pay Range: $25 - $36/hour" or "Pay Rate: $25–$26 per hour"
        r'[Pp]ay\s+[Rr]ange[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*(?:per\s+)?hours?',
        r'[Pp]ay\s+[Rr]ate[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*(?:per\s+)?hours?',
        r'[Ss]alary[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*(?:per\s+)?hours?',
        r'[Cc]ompensation[^$]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*hours?',
        # "$25 - $36/hour" or "$50-$75 per hour"
        r'\$(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*/?\s*(?:per\s+)?hours?',
        r'\$(\d+(?:[\.,]\d+)?)\s*[-–—]\s*(\d+(?:[\.,]\d+)?)\s*(?:per\s+)?hours?',
        # "$25–$36/hr" 
        r'\$(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*/?\s*hrs?',
    ]
    
    for pattern in pay_patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            if len(match.groups()) == 2:
                min_pay = match.group(1).replace(',', '')
                max_pay = match.group(2).replace(',', '')
                return f"${min_pay} - ${max_pay}/hour"
            elif len(match.groups()) == 1:
                return f"${match.group(1)}/hour"
    
    # Single rate patterns
    single_patterns = [
        r'[Pp]ay\s+[Rr]ate[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*/?\s*(?:per\s+)?hrs?',
        r'\$(\d+(?:[\.,]\d+)?)\s*/\s*(?:per\s+)?hrs?',
        r'\$(\d+(?:[\.,]\d+)?)\s*/?\s*(?:per\s+)?hours?'
    ]
    
    for pattern in single_patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return f"${match.group(1)}/hour"
    
    return ""

def get_fallback_posted_date() -> str:
    """Get a reasonable fallback posted date when no date info is available from JobDiva"""
    from datetime import datetime
    # Use today's date as fallback - jobs are typically posted recently
    return datetime.now().strftime("%b %d, %Y")

def extract_posted_date_from_text(description: str) -> str:
    """Extract posted date from job description text as fallback when structured fields are missing"""
    if not description:
        return ""
    
    # Look for various date patterns
    date_patterns = [
        r'[Pp]osted[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})',
        r'[Dd]ate[:\s]*(\w+\s+\d{1,2},?\s+\d{4})',
        r'[Ii]ssued[:\s]*(\w+\s+\d{1,2},?\s+\d{4})',
        r'[Cc]reated[:\s]*(\w+\s+\d{1,2},?\s+\d{4})',
        r'Job\s+ID[:\s]*\d+.*?[Pp]osted[:\s]*(\w+\s+\d{1,2},?\s+\d{4})'
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return match.group(1)
    





def calculate_date_duration(start_date_str: str, end_date_str: str) -> str:
    """Calculate human-readable duration between two date strings of format '%b %d, %Y'."""
    if not start_date_str or not end_date_str: 
        return ""
    try:
        from datetime import datetime
        start_dt = datetime.strptime(start_date_str, "%b %d, %Y")
        end_dt = datetime.strptime(end_date_str, "%b %d, %Y")
        
        if end_dt < start_dt:
            return ""
            
        total_days = (end_dt - start_dt).days + 1 # Inclusive
        if total_days <= 0:
            return ""
            
        years = total_days // 365
        rem_days = total_days % 365
        
        months = int(rem_days / 30.436875)
        days_diff = round(rem_days - (months * 30.436875))
            
        parts = []
        if years > 0:
            parts.append(f"{years} year" if years == 1 else f"{years} years")
        if months > 0:
            parts.append(f"{months} month" if months == 1 else f"{months} months")
        if days_diff > 0:
            parts.append(f"{days_diff} day" if days_diff == 1 else f"{days_diff} days")
            
        return " ".join(parts) if parts else "0 days"
    except Exception:
        return ""

_VERSION_SUFFIX_RE = re.compile(r"-v\d+$")


def strip_job_version_suffix(ref: Any) -> Optional[str]:
    """Reduce an internal versioned job reference to its root JobDiva reference.

    Versioned refs (e.g. ``26-06182-v2``) are LOCAL clones created by "Edit Job
    Setup" after launch — JobDiva itself only knows the root ref ``26-06182``.
    The ``-vN`` suffix is a display / internal-relations concept (it keeps each
    version's candidate bucket separate); it must NEVER be sent to JobDiva.

    Use this ONLY when a value is about to become a JobDiva HTTP payload
    (jobdivaref / jobOrderId / updateJob). Do NOT use it for local DB row
    resolution (``WHERE job_id = ...`` / ``WHERE jobdiva_id = ...``) — there the
    full versioned ref is the real key and stripping it would clobber v1.
    """
    if ref is None:
        return None
    return _VERSION_SUFFIX_RE.sub("", str(ref).strip())


def normalize_jobdiva_date(date_val: Any) -> str:
    """
    Format JobDiva date/timestamp into a readable YYYY-MM-DD format.
    Handles numeric timestamps and ISO date strings.
    Fixed to handle 2-digit years correctly relative to current year.
    Added validation to skip invalid date formats.
    """
    if not date_val:
        return ""
    
    # Handle numeric timestamp (milliseconds)
    if isinstance(date_val, (int, float)) or (isinstance(date_val, str) and date_val.isdigit()):
        try:
            ts = int(date_val)
            if ts > 10**11: # Likely milliseconds
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")
        except:
            pass

    # Handle string date formats
    date_str = str(date_val).strip()
    if not date_str:
        return ""
    
    # VALIDATION: Skip obviously invalid date formats like "2024/25"
    # Check for invalid patterns that would cause parsing errors
    if re.match(r'^\d{4}/\d{2}$', date_str):  # Pattern like "2024/25"
        return ""  # Skip this invalid format
    
    # Handle 2-digit year patterns first with smart year interpretation
    current_year = datetime.now().year
    
    two_digit_patterns = [
        "%m/%d/%y %I:%M %p",  # "02/24/26 9:52 AM"
        "%m/%d/%y",           # "02/24/26"  
    ]
    
    for pattern in two_digit_patterns:
        try:
            dt = datetime.strptime(date_str, pattern)
            # Python's %y interprets 00-68 as 2000-2068, 69-99 as 1969-1999
            # For our use case in 2026, this is already correct for recent dates
            # No adjustment needed since 26 -> 2026 is correct
            return dt.strftime("%b %d, %Y")
        except:
            continue
    
    # JobDiva 4-digit year patterns
    four_digit_patterns = [
        "%m/%d/%Y",           # "02/24/2024"
        "%m/%d/%Y %I:%M %p",  # "02/24/2024 9:52 AM"
    ]
    
    for pattern in four_digit_patterns:
        try:
            dt = datetime.strptime(date_str, pattern)
            return dt.strftime("%b %d, %Y")
        except:
            continue
    
    # Standard ISO and other formats    
    standard_patterns = [
        "%Y-%m-%d %H:%M:%S", 
        "%Y-%m-%dT%H:%M:%S", 
        "%Y-%m-%d"
    ]
    
    for pattern in standard_patterns:
        try:
            # We first parse then format to "Mar 18, 2026"
            dt = datetime.strptime(date_str[:19].replace('T', ' '), "%Y-%m-%d %H:%M:%S" if ' ' in date_str[:19] else "%Y-%m-%d")
            return dt.strftime("%b %d, %Y")
        except:
            try:
                # Fallback for common formats
                dt = datetime.fromisoformat(date_str.split('.')[0])
                return dt.strftime("%b %d, %Y")
            except:
                continue
                
    # Return empty string (not original input) if all parsing fails
    # This allows fallback logic to work properly 
    return ""

def extract_multiple_recruiter_emails(data: Dict[str, Any]) -> List[str]:
    """
    Extract multiple recruiter email addresses from JobDiva API response.
    Looks for various field patterns that might contain recruiter emails.
    """
    emails = []
    
    # Common JobDiva fields that might contain recruiter emails
    email_fields = [
        "recruiterEmail", "recruiter_email", "recruiterEmails", "recruiter_emails",
        "ownerEmail", "owner_email", "assignedRecruiterEmail", "assigned_recruiter_email",
        "accountManagerEmail", "account_manager_email", "contactEmail", "contact_email",
        "primaryRecruiterEmail", "primary_recruiter_email", "salesRepEmail", "sales_rep_email"
    ]
    
    for field in email_fields:
        value = get_field(data, [field])
        if value:
            if isinstance(value, str):
                # Single email or comma-separated emails
                split_emails = [email.strip() for email in value.split(',') if email.strip()]
                emails.extend(split_emails)
            elif isinstance(value, list):
                # List of emails
                emails.extend([str(email).strip() for email in value if email])
    
    # Remove duplicates and invalid emails, maintain order
    seen = set()
    valid_emails = []
    for email in emails:
        email = email.strip().lower()
        if email and '@' in email and '.' in email and email not in seen:
            seen.add(email)
            valid_emails.append(email)
    
    return valid_emails

def normalize_employment_type(emp_type: str) -> str:
    """
    Normalize JobDiva employment types to standard application format.
    Maps various JobDiva values to: W2, 1099, C2C, Full-Time, Contract
    """
    if not emp_type:
        return ""
    
    emp_lower = emp_type.lower().strip()
    
    # Map direct placement to Full-Time as requested
    if "direct placement" in emp_lower or "direct" in emp_lower:
        return "Full-Time"
    
    # Map other common JobDiva employment types
    if "full" in emp_lower and "time" in emp_lower:
        return "Full-Time"
    if "part" in emp_lower and "time" in emp_lower:
        return "Part-Time"
    if "contract" in emp_lower:
        return "Contract"
    if "w2" in emp_lower or "w-2" in emp_lower:
        return "W2"
    if "1099" in emp_lower:
        return "1099"
    if "c2c" in emp_lower or "corp to corp" in emp_lower or "corp-to-corp" in emp_lower:
        return "C2C"
    if "temp" in emp_lower and ("to" in emp_lower or "perm" in emp_lower):
        return "Contract"
    if "permanent" in emp_lower or "perm" in emp_lower:
        return "Full-Time"
    
    # Return original if no mapping found
    return emp_type

class JobDivaService:
    def _extract_customer_from_description(self, description: str) -> str:
        """Last resort: try to extract customer name from the description text."""
        if not description: return None
        
        # Look for patterns like "Client: [Name]" or "Company: [Name]" or "Customer: [Name]"
        patterns = [
            r"(?i)client:\s*([^\n\r<]+)",
            r"(?i)company:\s*([^\n\r<]+)",
            r"(?i)customer:\s*([^\n\r<]+)",
            r"(?i)hiring company:\s*([^\n\r<]+)"
        ]
        
        for p in patterns:
            match = re.search(p, description[:1000]) # Only check first 1000 chars
            if match:
                name = match.group(1).strip()
                # Basic cleanup
                name = re.sub(r'<[^>]*>', '', name)
                if len(name) > 2 and len(name) < 100:
                    return name
        return None
    def get_local_job(self, job_id: str) -> Optional[dict]:
        if not self.engine:
            return None
        try:
            with self.engine.connect() as conn:
                res = conn.execute(text("SELECT * FROM monitored_jobs WHERE job_id = :job_id"), {"job_id": job_id})
                row = res.fetchone()
                if row:
                    # Map row mapping to dict
                    return dict(row._mapping)
        except Exception as e:
            logger.error(f"Error fetching local job {job_id}: {e}")
        return None

    def __init__(self):
        self.api_url = JOBDIVA_API_URL
        self.client_id = JOBDIVA_CLIENT_ID
        self.username = JOBDIVA_USERNAME
        self.password = JOBDIVA_PASSWORD
        self.cached_token = None
        self.token_expiry = 0
        
        self.db_url = DATABASE_URL
        self.engine = None
        if self.db_url:
            try:
                # v22: add pool sizing + pre_ping + connect_timeout. Pre-v22 a
                # slow DB connect hung uvicorn workers for TCP default ~2 min;
                # unpooled defaults also leaked connections under load.
                self.engine = sqlalchemy.create_engine(
                    self.db_url,
                    pool_size=5,
                    max_overflow=10,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    connect_args={"connect_timeout": 5},
                )
            except Exception as e:
                logger.error(f"Failed to create JobDiva DB engine: {e}")

    async def authenticate(self, force_refresh: bool = False) -> str:
        """Authenticate with JobDiva and return JWT token."""
        if force_refresh:
            self.cached_token = None
            self.token_expiry = 0

        if self.cached_token and time.time() < self.token_expiry:
            return self.cached_token
        
        if not self.client_id or not self.username:
            logger.error(f"JobDiva Credentials not configured properly.")
            return None

        auth_url = f"{self.api_url}/api/authenticate"
        params = {
            "clientid": self.client_id,
            "username": self.username,
            "password": self.password
        }

        _auth_delays = [2, 4]
        for _attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    debug_log(f"Authenticating to JobDiva: {self.username} at {auth_url} (attempt {_attempt+1}/3)")
                    response = await client.get(auth_url, params=params)

                if response.status_code != 200:
                    debug_log(f"JobDiva Auth Failed: {response.status_code} - {response.text}")
                    if _attempt < 2:
                        await asyncio.sleep(_auth_delays[_attempt])
                        continue
                    return None

                token = response.text.replace("\"", "").strip()
                if len(token) < 10:
                    logger.error(f"JobDiva Auth returned invalid token: {token}")
                    return None

                self.cached_token = token
                self.token_expiry = time.time() + (23 * 3600)
                debug_log("JobDiva Auth Successful")
                return token

            except Exception as e:
                logger.error(f"JobDiva Auth Exception (attempt {_attempt+1}/3): {repr(e)}")
                if _attempt < 2:
                    await asyncio.sleep(_auth_delays[_attempt])
                    continue
        return None

    async def search_candidates(
        self,
        skills: List[Any],
        location: str,
        page: int = 1,
        limit: int = 100,
        job_id: str = None,
        boolean_string: str = "",
        recent_days: Optional[int] = None,
        require_resume: bool = True,
        countries: Optional[List[str]] = None,
        states: Optional[List[str]] = None,
        page_number: int = 0,
        zip_code: str = "",
        within_miles: Optional[int] = None,
        title: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Search for candidates.
        - If job_id provided: Search applicants to that specific job (with optional filtering)
        - If no job_id: Search the general Talent Pool based on skills and location

        New in April 2026:
        - `recent_days`: if set, JobDiva Talent Search is constrained to candidates
          whose LASTMODIFIED is within the last N days (embedded inside the
          boolean via jobdiva_boolean_translator).
        - `require_resume`: when True (default), Talent Search results without
          resume text/file are dropped before returning. Recruiters opted into
          "Include candidates without resumes" pass False.
        """
        token = await self.authenticate()
        if not token: return []

        # If job_id provided, search for applicants to that specific job (with filtering)
        if job_id:
            return await self._search_job_applicants(job_id, limit, token, skills, location)

        # Talent pool search
        logger.debug("Searching JobDiva general talent pool")
        return await self._search_talent_pool(
            skills, location, limit, token,
            boolean_string=boolean_string,
            recent_days=recent_days,
            require_resume=require_resume,
            countries=countries or [],
            states=states or [],
            page_number=page_number or 0,
            zip_code=zip_code or "",
            within_miles=within_miles,
            title=title,
        )

    async def _search_job_applicants(self, job_id: str, limit: int, token: str, skills: List[Any] = None, location: str = "") -> List[Dict[str, Any]]:
        """
        Search for candidates who applied to a specific job.
        Location constraints removed - only skills filtering if needed.
        """
        # Always get all job applicants - location constraints removed
        logger.debug(f"Getting all JobDiva applicants for job_id={job_id}")
        return await self._get_all_job_applicants(job_id, limit, token)

    async def _get_all_job_applicants(self, job_id: str, limit: int, token: str) -> List[Dict[str, Any]]:
        """Get all candidates who applied to a specific job using JobDiva v2 API."""
        resolved = await self._resolve_jobdiva_job_id(job_id)
        safe_id = str(resolved) if resolved is not None else str(job_id)

        logger.debug(f"Getting JobDiva applicants for job_id={job_id}, safe_id={safe_id}")
        
        # Use only the working JobApplicantsDetail endpoint
        endpoint_url = f"{self.api_url}/apiv2/bi/JobApplicantsDetail?jobId={safe_id}"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        jd_results = []
        
        try:
            logger.debug(f"Trying JobDiva applicants endpoint: {endpoint_url}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(endpoint_url, headers=headers)
                
                logger.debug(f"Job applicants API response: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    logger.debug(f"Raw applicants data type: {type(data)}, keys: {list(data.keys()) if isinstance(data, dict) else 'Not dict'}")
                    
                    # Handle JobApplicantsDetail response format: {'message': '', 'data': [candidates]}
                    applicants = []
                    if isinstance(data, dict) and "data" in data:
                        applicants = data["data"] or []
                    elif isinstance(data, list):
                        applicants = data
                    elif isinstance(data, dict):
                        # Fallback: try other possible keys
                        applicants = (data.get("applicants") or 
                                    data.get("candidates") or 
                                    data.get("results") or [])
                    
                    logger.debug(f"Extracted {len(applicants)} applicants from JobApplicantsDetail")
                    
                    for c in applicants:
                        # Use correct field names from JobApplicantsDetail response
                        first_name = get_field(c, ["FIRSTNAME", "firstName", "firstname"]) or "Unknown"
                        last_name = get_field(c, ["LASTNAME", "lastName", "lastname"]) or "Candidate"
                        if not is_valid_candidate_name(first_name, last_name):
                            logger.warning("Dropping JobDiva applicant with invalid name: first=%r last=%r id=%r", first_name, last_name, get_field(c, ["CANDIDATEID", "candidateId", "id", "ID", "canId"]))
                            continue
                        full_name = f"{first_name} {last_name}".strip()

                        # Extract candidate ID using correct field name
                        candidate_id = get_field(c, ["CANDIDATEID", "candidateId", "id", "ID", "canId"]) or "Unknown"
                        
                        # Simple default score 
                        match_score = self._calculate_match_score(c, [])
                        
                        # Extract candidate skills
                        candidate_skills = self._extract_candidate_skills(c)
                        
                        home_city = get_field(c, ["CITY", "city", "locationCity"]) or ""
                        home_state = get_field(c, ["STATE", "state", "locationState"]) or ""
                        home_zip = get_field(c, ["ZIPCODE", "zipcode", "ZIP", "zip", "postalCode", "POSTALCODE"]) or ""
                        work_city = get_field(c, ["workCity", "WORKCITY"]) or ""
                        work_state = get_field(c, ["workState", "WORKSTATE"]) or ""
                        work_location_str = ", ".join(p for p in [work_city, work_state] if p).strip()
                        home_location_str = ", ".join(p for p in [home_city, home_state] if p).strip()

                        jd_results.append({
                            "candidate_id": str(candidate_id),  # Add this field for consistency
                            "id": str(candidate_id),
                            "name": full_name,
                            "first_name": first_name,  # Use underscore format
                            "last_name": last_name,    # Use underscore format
                            "firstName": first_name,
                            "lastName": last_name,
                            "email": _get_candidate_email(c),
                            "city": home_city,
                            "state": home_state,
                            "zipcode": home_zip,
                            "location": home_location_str,
                            "work_city": work_city,
                            "work_state": work_state,
                            "work_location": work_location_str,
                            "title": get_field(c, ["TITLE", "title", "candidateTitle", "jobTitle"]) or "",
                            "source": "JobDiva-Applicants",
                            "match_score": match_score,
                            "skills": candidate_skills,
                            "experience_years": self._extract_experience_years(c),
                            "resume_text": self._extract_resume_text(c),
                            "resume_id": get_field(c, ["RESUMEID", "resumeId", "resume_id"]),
                            "received": get_field(c, ["RECEIVED", "received"]),
                            "available": get_field(c, ["AVAILABLE", "available", "STATUS", "status"]),
                            "availability_status": get_field(c, ["AVAILABLE", "available", "STATUS", "status"]),
                            "employee_status": get_field(c, ["EMPLOYEESTATUS", "employeeStatus", "CURRENTEMPLOYEE", "currentEmployee", "ASSIGNMENTSTATUS", "assignmentStatus"]),
                            "lastnote": get_field(c, ["LASTNOTE", "lastNote"]),
                            "phone": _get_candidate_phone(c)
                        })
                    
                    if jd_results:
                        logger.debug(f"Got {len(jd_results)} applicants from JobApplicantsDetail")
                else:
                    logger.warning(f"❌ Endpoint failed with status: {response.status_code}, response: {response.text[:200]}")
                    
        except Exception as e:
            logger.error(f"❌ Exception with endpoint {endpoint_url}: {e}")
        
        return jd_results

    async def _resolve_jobdiva_job_id(self, local_or_ref_id: Optional[str]) -> Optional[int]:
        """Coerce an internal job_id (numeric or reference like '18-25601') to a JobDiva integer ID."""
        if not local_or_ref_id:
            return None
        s = str(local_or_ref_id).strip()
        if not s:
            return None
        # Strip an internal version suffix (e.g. "26-06182-v2" -> "26-06182").
        # Versioned jobs are local clones used for re-editing after launch; they
        # share the original JobDiva job, so sourcing must resolve against the
        # un-versioned reference.
        s = strip_job_version_suffix(s)
        if "-" not in s:
            try:
                return int(s)
            except (TypeError, ValueError):
                return None
        try:
            job_info = await self.get_job_by_id(s)
        except Exception as e:
            logger.warning(f"_resolve_jobdiva_job_id: get_job_by_id failed for {s!r}: {e}")
            return None
        if not job_info:
            return None
        resolved = get_field(job_info, ["id", "jobId", "JOBID", "ID"])
        try:
            return int(str(resolved).strip()) if resolved else None
        except (TypeError, ValueError):
            return None

    async def search_via_job_agent(
        self,
        job_id: Optional[str],
        resume_count: int = 200,
        require_resume: bool = True,
    ) -> Dict[str, Any]:
        """Talent-pool sourcing using JobDiva's JobAgentSearch matcher.

        NOTE: JobDiva's job-level AI criteria fields (SKILLS, AGENT_SEARCH_TITLE)
        cannot be set via the public API. Tested updateJob.skills (5 shapes),
        direct field-name overrides for SKILLS/agentSearchTitle/agentSkills/
        AGENT_SEARCH_TITLE/searchAgentSkills/etc., createJobNote, description,
        title, postingtitle, nested agentSearch, userfields — all silently
        accepted, none persist. Setting criteria requires JobDiva web UI today.
        Until we capture the real "Save Search Agent" endpoint via browser-trace,
        this method may return empty + `criteria_unconfigured=True` for
        otherwise-valid jobs.

        Returns:
            {
                "candidates": [...],         # ranked candidates (may be empty)
                "criteria_unconfigured": bool,  # True iff JobDiva said "Criteria Not Assigned"
                "resolved_jobdiva_id": int|None, # for logging
            }
        """
        token = await self.authenticate()
        if not token:
            return {"candidates": [], "criteria_unconfigured": False, "resolved_jobdiva_id": None}
        jdiva_id = await self._resolve_jobdiva_job_id(job_id)
        if jdiva_id is None:
            logger.info(
                f"JobAgentSearch skipped: could not resolve JobDiva jobId from {job_id!r}"
            )
            return {"candidates": [], "criteria_unconfigured": False, "resolved_jobdiva_id": None}
        candidates, criteria_unconfigured = await self._search_with_job_agent(
            job_id=jdiva_id,
            resume_count=int(resume_count),
            token=token,
            require_resume=require_resume,
        )
        return {
            "candidates": candidates,
            "criteria_unconfigured": criteria_unconfigured,
            "resolved_jobdiva_id": jdiva_id,
        }

    async def _search_with_job_agent(
        self,
        job_id: int,
        resume_count: int,
        token: str,
        require_resume: bool = True,
    ) -> tuple:
        """Call /apiv2/jobdiva/JobAgentSearch and normalize the response.

        Returns `(candidates_list, criteria_unconfigured_bool)`. The flag is
        True when JobDiva returns 500 with "Criteria Not Assigned" — that
        signals the recruiter never configured the AI matcher for this job
        in JobDiva's web UI. Caller should fall back to TalentSearch and
        surface the flag to the frontend so a UI nudge can prompt the
        recruiter to set criteria.

        Field shape differs from TalentSearch: STATE → PROVINCE, no EMAIL —
        emails fill in via the CandidatesDetail enrichment merge below.
        """
        url = f"{self.api_url}/apiv2/jobdiva/JobAgentSearch"
        try:
            params = {"jobId": int(job_id), "resumeCount": int(resume_count)}
        except (ValueError, TypeError):
            # If job_id is still a string with hyphen, try to clean it
            safe_id = "".join(filter(str.isdigit, str(job_id)))
            params = {"jobId": int(safe_id) if safe_id else 0, "resumeCount": int(resume_count)}
        headers = {"Authorization": f"Bearer {token}"}

        jd_results: List[Dict[str, Any]] = []
        profile_only_results: List[Dict[str, Any]] = []
        dropped_no_resume = 0
        criteria_unconfigured = False

        # Retry schedule: first attempt 5 min, then two more at 10 min each.
        _ja_timeouts = [300.0, 600.0, 600.0]
        _ja_delays = [5, 15]
        response = None
        _last_response = None  # saved even on 5xx, for criteria-unconfigured check

        # --- timing instrumentation (observation only; no behavior change) ---
        # Splits the app-side wall-clock into the segments Postman never pays
        # for: connection setup + HTTP, retry backoff, JSON parse, and the
        # synchronous candidate-normalization loop. Grep "JobAgent TIMING".
        _t_start = time.perf_counter()
        _request_ms = 0.0    # cumulative setup+http across attempts
        _sleep_ms = 0.0      # cumulative retry backoff (asyncio.sleep)
        _json_ms = 0.0       # response.json() deserialization
        _normalize_ms = 0.0  # per-candidate normalization loop
        _attempts_made = 0
        _resp_bytes = 0
        try:
            for _attempt, _timeout_val in enumerate(_ja_timeouts):
                try:
                    # Time client creation (fresh DNS+TCP+TLS handshake — no
                    # connection reuse) together with the GET, since that whole
                    # cost is what Postman avoids via a warm keep-alive socket.
                    _attempts_made += 1
                    _req_t0 = time.perf_counter()
                    async with httpx.AsyncClient(timeout=_timeout_val) as client:
                        logger.info(
                            "JobAgentSearch jobId=%s attempt %d/%d timeout=%.0fs",
                            job_id, _attempt + 1, len(_ja_timeouts), _timeout_val,
                        )
                        response = await client.get(url, params=params, headers=headers)
                        _req_ms = (time.perf_counter() - _req_t0) * 1000.0
                    _request_ms += _req_ms
                    _last_response = response
                    try:
                        _resp_bytes = len(response.content)
                    except Exception:
                        _resp_bytes = 0
                    logger.info(
                        "JobAgentSearch jobId=%s attempt %d: HTTP %d in %.0fms (setup+http), %d bytes",
                        job_id, _attempts_made, response.status_code, _req_ms, _resp_bytes,
                    )
                    if response.status_code < 500:
                        break
                    body = response.text or ""
                    logger.warning(
                        "JobAgentSearch attempt %d/%d: HTTP %d — %s",
                        _attempt + 1, len(_ja_timeouts), response.status_code, body[:200],
                    )
                    response = None
                except (httpx.TimeoutException, httpx.ConnectError) as _e:
                    logger.warning(
                        "JobAgentSearch attempt %d/%d failed: %s",
                        _attempt + 1, len(_ja_timeouts), _e,
                    )
                    response = None

                if _attempt < len(_ja_timeouts) - 1:
                    _delay = _ja_delays[_attempt]
                    logger.info("JobAgentSearch retrying in %ds...", _delay)
                    _sleep_t0 = time.perf_counter()
                    await asyncio.sleep(_delay)
                    _sleep_ms += (time.perf_counter() - _sleep_t0) * 1000.0
                else:
                    logger.error(
                        "JobAgentSearch all %d attempts exhausted for jobId=%s",
                        len(_ja_timeouts), job_id,
                    )
        except Exception as e:
            logger.error(f"JobAgentSearch error: {e}")
            return [], criteria_unconfigured

        if response is None:
            # All retries exhausted — check last response for criteria-unconfigured signal.
            if _last_response is not None:
                _body = _last_response.text or ""
                if _is_job_agent_criteria_unconfigured(_last_response.status_code, _body):
                    criteria_unconfigured = True
                    logger.info(
                        "JobAgentSearch jobId=%s: criteria not configured in JobDiva; surfacing to UI.",
                        job_id,
                    )
            logger.info(
                "JobAgent TIMING jobId=%s resumeCount=%s attempts=%d FAILED (no usable response) | "
                "request_ms=%.0f sleep_ms=%.0f total_ms=%.0f",
                job_id, resume_count, _attempts_made,
                _request_ms, _sleep_ms, (time.perf_counter() - _t_start) * 1000.0,
            )
            return [], criteria_unconfigured

        if response.status_code != 200:
            body = response.text or ""
            if _is_job_agent_criteria_unconfigured(response.status_code, body):
                criteria_unconfigured = True
                logger.info(
                    f"JobAgentSearch jobId={job_id}: criteria not configured "
                    f"in JobDiva ({response.status_code}); will fall back."
                )
            else:
                logger.warning(
                    f"JobAgentSearch failed: {response.status_code} - {body[:200]}"
                )
            logger.info(
                "JobAgent TIMING jobId=%s resumeCount=%s attempts=%d status=%d (non-200) | "
                "request_ms=%.0f sleep_ms=%.0f total_ms=%.0f resp_bytes=%d",
                job_id, resume_count, _attempts_made, response.status_code,
                _request_ms, _sleep_ms, (time.perf_counter() - _t_start) * 1000.0, _resp_bytes,
            )
            return [], criteria_unconfigured

        try:
            _json_t0 = time.perf_counter()
            data = response.json()
            _json_ms = (time.perf_counter() - _json_t0) * 1000.0
            candidates = data.get("data") if isinstance(data, dict) else data
            candidates = candidates or []
        except Exception as e:
            logger.error(f"JobAgentSearch error: {e}")
            return [], criteria_unconfigured

        _norm_t0 = time.perf_counter()
        if candidates:
            logger.info(f"JobAgentSearch RAW CANDIDATE KEYS: {list(candidates[0].keys())}")
        for api_rank, c in enumerate(candidates):
            candidate_id = str(
                get_field(c, ["candidateId", "CANDIDATEID", "id", "ID"]) or ""
            )
            if not candidate_id:
                continue

            first_name = get_field(c, ["firstName", "firstname", "FIRSTNAME"]) or "Unknown"
            last_name = get_field(c, ["lastName", "lastname", "LASTNAME"]) or "Candidate"

            # Targeted debug for Carol Lynn
            if "carol" in first_name.lower() and "lynn" in last_name.lower():
                logger.info(f"CAROL_LYNN_RAW candidate_id={candidate_id} raw_keys={list(c.keys())} status={c.get('status')} available={c.get('available')} qualifications={c.get('qualifications')}")

            if not is_valid_candidate_name(first_name, last_name):
                logger.warning("Dropping JobAgent candidate with invalid name: first=%r last=%r id=%r", first_name, last_name, candidate_id)
                continue
            full_name = f"{first_name} {last_name}".strip()

            # PROVINCE is JobAgentSearch's name for state.
            city = get_field(c, ["city", "CITY", "locationCity"]) or ""
            state = (
                get_field(c, ["state", "STATE", "PROVINCE", "province", "locationState"])
                or ""
            )
            location_str = ", ".join(p for p in [city, state] if p).strip()

            resume_text = self._extract_resume_text(c)
            resume_id = get_field(c, ["resumeId", "RESUMEID", "resume_id"])
            has_resume = bool((resume_text or "").strip()) or bool(resume_id)

            abstract = get_field(c, ["ABSTRACT", "abstract", "summary", "SUMMARY"]) or ""
            if not abstract and resume_text:
                abstract = resume_text[:240].replace("\n", " ").strip()
            if abstract and len(abstract) > 240:
                abstract = abstract[:237].rstrip() + "..."

            record = {
                "candidate_id": candidate_id,
                "id": candidate_id,
                "name": full_name,
                "first_name": first_name,
                "last_name": last_name,
                "firstName": first_name,
                "lastName": last_name,
                "email": _get_candidate_email(c),
                "city": city,
                "state": state,
                "zipcode": get_field(c, ["zipcode", "ZIPCODE", "zip", "ZIP"]) or "",
                "location": location_str,
                "work_city": "",
                "work_state": "",
                "work_location": "",
                "title": get_field(c, ["title", "candidateTitle", "TITLE"]) or "",
                "source": "JobDiva-JobAgent",
                "api_rank": api_rank,
                "match_score": 75,
                "skills": self._extract_candidate_skills(c),
                "experience_years": self._extract_experience_years(c),
                "resume_text": resume_text,
                "resume_id": resume_id,
                "received": get_field(c, ["received", "RECEIVED"]),
                "available": get_field(c, ["available", "AVAILABLE"]) or "",
                "availability_status": get_field(c, ["available", "AVAILABLE"]) or "",
                "employee_status": get_field(
                    c,
                    [
                        "EMPLOYEESTATUS",
                        "employeeStatus",
                        "CURRENTEMPLOYEE",
                        "currentEmployee",
                        "ASSIGNMENTSTATUS",
                        "assignmentStatus",
                    ],
                ),
                "qualifications": get_field(c, ["qualifications", "QUALIFICATIONS"]) or [],
                "abstract": abstract,
                "lastnote": get_field(c, ["lastNote", "LASTNOTE"]),
                "phone": _get_candidate_phone(c),
            }

            # Map the numeric status field to employee_status string:
            # 0 = normal, 1 = Current Employee, 2 = Past Employee
            raw_status = get_field(c, ["status", "STATUS"])
            if raw_status is not None:
                try:
                    status_int = int(raw_status)
                    if status_int == 1 and not record.get("employee_status"):
                        record["employee_status"] = "Current Employee"
                        logger.info(f"JobAgent status=1 → marking candidate {candidate_id} as Current Employee")
                    elif status_int == 2 and not record.get("employee_status"):
                        record["employee_status"] = "Past Employee"
                except (ValueError, TypeError):
                    pass

            if require_resume and not has_resume:
                dropped_no_resume += 1
                record["resume_missing"] = True
                profile_only_results.append(record)
                continue
            jd_results.append(record)

        _normalize_ms = (time.perf_counter() - _norm_t0) * 1000.0
        logger.info(
            "JobAgent TIMING jobId=%s resumeCount=%s attempts=%d raw=%d kept=%d | "
            "request_ms=%.0f (setup+http) sleep_ms=%.0f json_ms=%.0f "
            "normalize_ms=%.0f total_ms=%.0f resp_bytes=%d",
            job_id, resume_count, _attempts_made, len(candidates),
            len(jd_results) + len(profile_only_results),
            _request_ms, _sleep_ms, _json_ms, _normalize_ms,
            (time.perf_counter() - _t_start) * 1000.0, _resp_bytes,
        )

        # Same enrichment + rescue flow as _search_talent_pool.
        merge_targets = jd_results + profile_only_results
        ids_to_enrich = [r["candidate_id"] for r in merge_targets if r.get("candidate_id")]
        from core import sourcing_config as _sc
        if ids_to_enrich and not _sc.FAST_PATH_SKIP_DETAIL_IN_TALENT_SEARCH:
            detail_t0 = time.time()
            detail_map = await self._fetch_candidate_details_batch(token, ids_to_enrich)
            detail_ms = int((time.time() - detail_t0) * 1000)
            counters = {"email": 0, "phone": 0, "address1": 0, "linkedin": 0, "resume": 0}
            rescued = 0
            for record in merge_targets:
                detail = detail_map.get(str(record.get("candidate_id") or ""))
                if not detail:
                    continue
                self._merge_detail_into_candidate(record, detail, counters)
                if record.get("resume_missing") and (record.get("resume_text") or record.get("resume_id")):
                    record.pop("resume_missing", None)
                    rescued += 1
            logger.debug(
                f"JobAgent CandidatesDetail enrichment: "
                f"{len(detail_map)}/{len(ids_to_enrich)} matched in {detail_ms}ms, "
                f"rescued={rescued}, fields_from_detail={counters}"
            )
        elif ids_to_enrich:
            logger.info(
                "FAST_PATH_SKIP_DETAIL: JobAgent path skipping inline CandidatesDetail for %d candidates; background hydration will follow.",
                len(ids_to_enrich),
            )

        if require_resume:
            promoted = [r for r in profile_only_results if not r.get("resume_missing")]
            if promoted:
                jd_results.extend(promoted)
                profile_only_results = [r for r in profile_only_results if r.get("resume_missing")]
                dropped_no_resume = max(0, dropped_no_resume - len(promoted))

        # POLICY: a JobDiva candidate is NEVER dropped for a missing résumé.
        # Always re-add still-resumeless profile-only candidates (flagged
        # `resume_missing` so the scorer/UI can downweight, never hide) —
        # unconditionally, so this can't regress on a config flag or on the
        # détail/résumé rescue still running in the background.
        if require_resume and profile_only_results:
            jd_results.extend(profile_only_results)
            logger.info(
                f"keep-no-resume: appended {len(profile_only_results)} "
                f"profile-only candidate(s) (JobAgent), flagged resume_missing"
            )
            profile_only_results = []
            dropped_no_resume = 0

        if require_resume and not jd_results and profile_only_results:
            jd_results = profile_only_results[: resume_count]
            logger.warning(
                "JobAgentSearch fallback activated: strict require_resume "
                "yielded 0 results, returning %s profile-only candidate(s)",
                len(jd_results),
            )

        logger.info(
            f"JobDiva search: source=JobAgent jobId={job_id} "
            f"raw={len(candidates)} returned={len(jd_results)} "
            f"dropped_no_resume={dropped_no_resume}"
        )
        return jd_results, criteria_unconfigured

    async def _search_talent_pool(
        self,
        skills: List[Any],
        location: str,
        limit: int,
        token: str,
        boolean_string: str = "",
        recent_days: Optional[int] = None,
        require_resume: bool = True,
        countries: Optional[List[str]] = None,
        states: Optional[List[str]] = None,
        page_number: int = 0,
        zip_code: str = "",
        within_miles: Optional[int] = None,
        title: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Search the JobDiva Talent Pool via the v2 TalentSearch contract.

        Live-probed 2026-07-19 (scripts/jobdiva_payload_variants_probe.py +
        swagger group "Version 2"): the endpoint takes the TalentSearchDef
        fields at the TOP LEVEL of the request body. The previous
        `{"talentSearchDef": {...}}` wrapper (plus string-typed
        skills/states/countries) was silently discarded by the server, which
        then returned its default unfiltered dump — the same ~2.5k candidates
        for every job, regardless of skills or location. Verified field
        semantics:

          skills               array of PLAIN terms, AND semantics; boolean
                               syntax inside a term kills the request
          zipCode/withinMiles  honored (98.8% in-radius vs 8.1% unfiltered)
          states / countries   arrays of 2-letter codes, honored
          titleSearch          honored alone; no extra effect beside skills
          advancedSkills, location   always return 0 rows — never send
          pageNumber/pageSize  ignored — the full set returns in one call

        Boolean OR / NOT / years clauses cannot be expressed server-side;
        they stay client-side in the scorer (see extract_and_terms).

        Also filters out profile-only candidates (no resume_text) unless the
        caller explicitly opts in via `require_resume=False`. These profiles
        are what triggered the "This candidate's resume is not available"
        warning in the UI and eroded trust in the match ranking.
        """
        from services.jobdiva_boolean_translator import (
            extract_and_terms,
            sanitize_talent_term,
            count_location_clauses,
        )
        from core import sourcing_config as _sc

        jd_results = []

        # Must-terms for the server-side AND: prefer the structured skills
        # payload (wizard chips); fall back to parsing the raw boolean.
        max_terms = max(1, int(getattr(_sc, "JOBDIVA_TALENT_MAX_SKILL_TERMS", 4) or 4))
        must_terms: List[str] = []
        seen_terms = set()
        for skill in skills or []:
            if isinstance(skill, dict):
                if skill.get("match_type", "must") == "exclude":
                    continue
                raw_term = skill.get("value") or skill.get("name") or ""
            else:
                raw_term = str(skill)
            term = sanitize_talent_term(raw_term)
            if term and term.lower() not in seen_terms:
                seen_terms.add(term.lower())
                must_terms.append(term)
        if not must_terms and boolean_string:
            must_terms = extract_and_terms(boolean_string, max_terms=max_terms)
        must_terms = must_terms[:max_terms]

        base_body: Dict[str, Any] = {
            "pageNumber": int(page_number or 0),
            "pageSize": limit,
        }
        countries_list = [str(c).strip().upper() for c in (countries or []) if str(c).strip()]
        base_body["countries"] = countries_list or ["US"]
        states_list = [str(s).strip().upper() for s in (states or []) if str(s).strip()]
        if states_list:
            base_body["states"] = states_list

        # Structured zip-radius. The zip rides ALONGSIDE any resolved states:
        # for an explicit-zip wizard location `states` is already empty (the
        # parser dropped it); when the zip was SYNTHESIZED from a plain
        # "City, ST" job the state IS the intended scope. Skipped for
        # multi-location searches: the structured field can only carry ONE
        # anchor, and pinning a multi-chip search to chip A's zip would
        # exclude chip B's candidates server-side.
        zip_radius_miles = 0
        zip5 = str(zip_code or "").strip()
        if (
            getattr(_sc, "JOBDIVA_ZIP_RADIUS_ENABLED", True)
            and len(zip5) == 5 and zip5.isdigit()
            and count_location_clauses(boolean_string or "") < 2
        ):
            # 2x headroom: the server radius is a coarse recall gate — it
            # must not empty the UI's BEYOND-radius soft-keep bucket. The
            # client-side verdict still measures true distance against the
            # recruiter's exact radius; this just cuts the wrong-coast noise
            # while keeping the near-miss band.
            zip_radius_miles = max(1, min(100, int(within_miles or 25) * 2))
            base_body["zipCode"] = zip5
            base_body["withinMiles"] = zip_radius_miles

        logger.debug(
            f"JobDiva Talent Search v2 — terms={must_terms!r} title={title!r} | "
            f"countries={base_body['countries']!r} states={base_body.get('states')!r} "
            f"zipCode={base_body.get('zipCode', '')!r} withinMiles={zip_radius_miles or ''}"
        )

        dropped_no_resume = 0
        profile_only_results: List[Dict[str, Any]] = []
        try:
            candidates = await self._fetch_talent_search_rows(
                token, base_body, must_terms, title=title
            )
            # The server ignores pageSize — cap client-side so the detail/
            # resume enrichment below stays bounded.
            candidates = candidates[: max(1, int(limit or 1))]
            for c in candidates:
                candidate_id = str(get_field(c, ["candidateId", "CANDIDATEID", "id", "ID"]) or "")
                if not candidate_id:
                    continue

                first_name = get_field(c, ["firstName", "firstname", "FIRSTNAME"]) or "Unknown"
                last_name = get_field(c, ["lastName", "lastname", "LASTNAME"]) or "Candidate"
                if not is_valid_candidate_name(first_name, last_name):
                    logger.warning("Dropping TalentSearch candidate with invalid name: first=%r last=%r id=%r", first_name, last_name, candidate_id)
                    continue
                full_name = f"{first_name} {last_name}".strip()

                resume_text = self._extract_resume_text(c)
                resume_id = get_field(c, ["resumeId", "RESUMEID", "resume_id"])
                has_resume = bool((resume_text or "").strip()) or bool(resume_id)

                # Filter out profile-only candidates unless caller opts in.
                # These trigger the "resume not available" warning downstream
                # and hurt the recruiter's trust in the match ranking.
                if require_resume and not has_resume:
                    dropped_no_resume += 1
                    # Keep a bounded fallback copy so we can avoid the
                    # "always empty" failure mode when all JobDiva hits
                    # are profile-only (common in some markets/roles).
                    profile_only_results.append({
                        "candidate_id": candidate_id,
                        "id": candidate_id,
                        "name": full_name,
                        "first_name": first_name,
                        "last_name": last_name,
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": get_field(c, ["email", "EMAIL"]) or "",
                        "city": get_field(c, ["city", "locationCity", "CITY"]) or "",
                        "state": get_field(c, ["state", "locationState", "STATE"]) or "",
                        "location": ", ".join([p for p in [
                            get_field(c, ["city", "locationCity", "CITY"]) or "",
                            get_field(c, ["state", "locationState", "STATE"]) or "",
                        ] if p]).strip(),
                        "work_city": "",
                        "work_state": "",
                        "work_location": "",
                        "title": get_field(c, ["title", "candidateTitle", "TITLE"]) or "",
                        "source": "JobDiva-TalentSearch",
                        "match_score": 75,
                        "skills": self._extract_candidate_skills(c),
                        "experience_years": self._extract_experience_years(c),
                        "resume_text": resume_text,
                        "resume_id": resume_id,
                        "received": get_field(c, ["received", "RECEIVED"]),
                        "recent_availability": get_field(
                            c,
                            [
                                "recentAvailability",
                                "RECENTAVAILABILITY",
                                "recent_availability",
                                "RECENT_AVAILABILITY",
                                "recentAvailable",
                                "RECENTAVAILABLE",
                                "recent_status",
                                "RECENT_STATUS",
                            ],
                        ) or "",
                        "available": get_field(c, ["available", "AVAILABLE", "availability", "AVAILABILITY", "status", "STATUS"]) or "",
                        "availability_status": get_field(c, ["available", "AVAILABLE", "availability", "AVAILABILITY", "status", "STATUS"]) or "",
                        "abstract": (
                            get_field(c, ["summary", "SUMMARY", "abstract", "ABSTRACT", "comments", "COMMENTS", "notes", "NOTES"])
                            or ((resume_text or "")[:240].replace("\n", " ").strip())
                        ),
                        "profile_url": get_field(c, ["profileUrl", "PROFILEURL", "profile_url", "PROFILE_URL"]),
                        "lastnote": get_field(c, ["lastNote", "LASTNOTE"]),
                        "phone": _get_candidate_phone(c),
                        "resume_missing": True,
                    })
                    continue

                city = get_field(c, ["city", "locationCity", "CITY"]) or ""
                state = get_field(c, ["state", "locationState", "STATE"]) or ""
                zipcode = get_field(c, ["zipcode", "ZIPCODE", "zip", "ZIP", "postalCode", "POSTALCODE"]) or ""
                location_str = ", ".join([p for p in [city, state] if p]).strip()

                # Abstract: prefer an explicit summary/comments field if JobDiva
                # returns one; fall back to the first ~200 chars of resume text
                # so the Step-5 list can show something meaningful.
                raw_abstract = (
                    get_field(c, ["summary", "SUMMARY", "abstract", "ABSTRACT", "comments", "COMMENTS", "notes", "NOTES"])
                    or ""
                )
                if not raw_abstract and resume_text:
                    raw_abstract = resume_text[:240].replace("\n", " ").strip()
                if raw_abstract and len(raw_abstract) > 240:
                    raw_abstract = raw_abstract[:237].rstrip() + "..."

                recent_availability = (
                    get_field(
                        c,
                        [
                            "recentAvailability",
                            "RECENTAVAILABILITY",
                            "recent_availability",
                            "RECENT_AVAILABILITY",
                            "recentAvailable",
                            "RECENTAVAILABLE",
                            "recent_status",
                            "RECENT_STATUS",
                        ],
                    )
                    or ""
                )
                availability_status = (
                    recent_availability
                    or get_field(c, ["available", "AVAILABLE", "availability", "AVAILABILITY", "status", "STATUS"])
                    or ""
                )
                profile_url = get_field(
                    c,
                    ["profileUrl", "PROFILEURL", "profile_url", "PROFILE_URL"],
                )

                jd_results.append({
                    "candidate_id": candidate_id,
                    "id": candidate_id,
                    "name": full_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "firstName": first_name,
                    "lastName": last_name,
                    "email": get_field(c, ["email", "EMAIL"]) or "",
                    "city": city,
                    "state": state,
                    "zipcode": zipcode,
                    "location": location_str,
                    "work_city": "",
                    "work_state": "",
                    "work_location": "",
                    "title": get_field(c, ["title", "candidateTitle", "TITLE"]) or "",
                    "source": "JobDiva-TalentSearch",
                    "match_score": 75,
                    "skills": self._extract_candidate_skills(c),
                    "experience_years": self._extract_experience_years(c),
                    "resume_text": resume_text,
                    "resume_id": resume_id,
                    "received": get_field(c, ["received", "RECEIVED"]),
                    "recent_availability": recent_availability,
                    "available": availability_status,
                    "availability_status": availability_status,
                    "employee_status": get_field(
                        c,
                        [
                            "EMPLOYEESTATUS",
                            "employeeStatus",
                            "CURRENTEMPLOYEE",
                            "currentEmployee",
                            "ASSIGNMENTSTATUS",
                            "assignmentStatus",
                        ],
                    ),
                    "abstract": raw_abstract,
                    "profile_url": profile_url,
                    "lastnote": get_field(c, ["lastNote", "LASTNOTE"]),
                    "phone": _get_candidate_phone(c),
                })

            # Two-step enrichment: TalentSearch returns thin records;
            # CandidatesDetail fills in address1, linkedinUrl, and any
            # email/phone/resume fields TalentSearch left empty. Merging
            # both jd_results AND profile_only_results so a detail-
            # supplied resume can rescue an otherwise-filtered candidate.
            merge_targets = jd_results + profile_only_results
            ids_to_enrich = [r["candidate_id"] for r in merge_targets if r.get("candidate_id")]
            from core import sourcing_config as _sc
            if ids_to_enrich and not _sc.FAST_PATH_SKIP_DETAIL_IN_TALENT_SEARCH:
                detail_t0 = time.time()
                detail_map = await self._fetch_candidate_details_batch(token, ids_to_enrich)
                detail_ms = int((time.time() - detail_t0) * 1000)
                rescued = 0
                fields_from_detail = {"email": 0, "phone": 0, "address1": 0, "linkedin": 0, "resume": 0}
                for record in merge_targets:
                    detail = detail_map.get(str(record.get("candidate_id") or ""))
                    if not detail:
                        continue
                    self._merge_detail_into_candidate(record, detail, fields_from_detail)
                    if record.get("resume_missing") and (record.get("resume_text") or record.get("resume_id")):
                        record.pop("resume_missing", None)
                        rescued += 1
                logger.debug(
                    f"CandidatesDetail enrichment: {len(detail_map)}/{len(ids_to_enrich)} matched "
                    f"in {detail_ms}ms, rescued={rescued}, "
                    f"fields_from_detail={fields_from_detail}"
                )
            elif ids_to_enrich:
                logger.info(
                    "FAST_PATH_SKIP_DETAIL: TalentSearch path skipping inline CandidatesDetail for %d candidates; background hydration will follow.",
                    len(ids_to_enrich),
                )

            # Second-pass: TalentSearch + CandidatesDetail still leave
            # `resume_text` empty for most candidates because resume bodies
            # live in CandidatesResumesDetail / ResumesTextDetail. Without
            # resume text the downstream skill scorer has nothing to match.
            # Fetch concurrently for the remaining empty-resume candidates.
            # When fast-path is on we defer resume fetch too — it's the
            # other big rate-limit consumer; background hydration handles it.
            empty_resume_ids = [
                r["candidate_id"] for r in (jd_results + profile_only_results)
                if r.get("candidate_id") and not (r.get("resume_text") or "").strip()
            ] if not _sc.FAST_PATH_SKIP_DETAIL_IN_TALENT_SEARCH else []
            if empty_resume_ids:
                resume_t0 = time.time()
                resume_map = await self._fetch_resume_text_batch(
                    token, empty_resume_ids[:200]
                )
                resume_ms = int((time.time() - resume_t0) * 1000)
                filled = 0
                for r in (jd_results + profile_only_results):
                    cid = r.get("candidate_id")
                    if not cid or (r.get("resume_text") or "").strip():
                        continue
                    body = resume_map.get(cid, "")
                    if body:
                        r["resume_text"] = body
                        if not (r.get("abstract") or "").strip():
                            r["abstract"] = body[:240].replace("\n", " ").strip()
                        # Resume backfill rescues profile-only candidates
                        # whose resume body now exists.
                        if r.get("resume_missing"):
                            r.pop("resume_missing", None)
                        filled += 1
                logger.debug(
                    f"Resume body backfill: {filled}/{len(empty_resume_ids)} "
                    f"populated in {resume_ms}ms"
                )

            # Promote rescued profile-only entries into the main result set.
            if require_resume:
                promoted = [r for r in profile_only_results if not r.get("resume_missing")]
                if promoted:
                    jd_results.extend(promoted)
                    profile_only_results = [r for r in profile_only_results if r.get("resume_missing")]
                    dropped_no_resume = max(0, dropped_no_resume - len(promoted))

            # POLICY: never drop a JobDiva candidate for a missing résumé.
            # Always append still-resumeless profile_only_results (flagged
            # `resume_missing`) so the scorer/UI can downweight them rather
            # than hide them — unconditional, not gated on a config flag.
            if require_resume and profile_only_results:
                jd_results.extend(profile_only_results)
                logger.info(
                    f"keep-no-resume: appended {len(profile_only_results)} "
                    f"profile-only candidate(s), flagged resume_missing"
                )
                profile_only_results = []
                dropped_no_resume = 0

            if dropped_no_resume:
                logger.info(
                    f"JobDiva Talent Search: dropped {dropped_no_resume} "
                    f"profile-only candidates (no resume). Toggle "
                    f"'Include candidates without resumes' on the UI to keep them."
                )

            # Safety fallback: if strict resume filtering removed everything,
            # return profile-only hits instead of an empty result set.
            if require_resume and not jd_results and profile_only_results:
                jd_results = profile_only_results[:limit]
                logger.warning(
                    "JobDiva Talent Search fallback activated: strict require_resume "
                    "yielded 0 results, returning %s profile-only candidate(s)",
                    len(jd_results),
                )

            logger.debug(f"JobDiva Talent Search returned {len(jd_results)} candidates")
        except Exception as e:
            logger.error(f"Talent Search Error: {e}")

        return jd_results

    async def _fetch_talent_search_rows(
        self,
        token: str,
        base_body: Dict[str, Any],
        must_terms: List[str],
        title: str = "",
    ) -> List[Dict[str, Any]]:
        """Run the v2 TalentSearch pulls and merge their raw rows.

        Pull 1: `skills` = AND of must_terms, relaxed to the two
        highest-priority terms when the full AND matches nothing (server
        AND semantics can zero out long must-lists). Pull 2: `titleSearch`
        recall pull — title matches surface candidates whose resume wording
        differs from the skill terms. Rows dedupe by candidateId, pull 1
        first. Never posts an empty search definition: the server answers
        one with its full unfiltered dump.
        """
        from core import sourcing_config as _sc

        url = f"{self.api_url}/apiv2/jobdiva/TalentSearch"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        async def _post(body: Dict[str, Any]) -> List[Dict[str, Any]]:
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        response = await client.post(url, json=body, headers=headers)
                    if response.status_code != 200:
                        logger.warning(
                            f"JobDiva Talent Search failed: {response.status_code} - {response.text[:200]}"
                        )
                        return []
                    data = response.json()
                    if isinstance(data, dict):
                        return data.get("data") or data.get("candidates") or data.get("results") or []
                    return data or []
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout) as exc:
                    # JobDiva intermittently truncates large chunked responses.
                    logger.warning(
                        f"JobDiva Talent Search transport retry {attempt + 1}/3: {exc!r}"
                    )
                    await asyncio.sleep(1.5 * (attempt + 1))
            return []

        rows: List[Dict[str, Any]] = []
        if must_terms:
            body = dict(base_body)
            body["skills"] = list(must_terms)
            rows = await _post(body)
            if not rows and len(must_terms) > 2:
                body["skills"] = list(must_terms[:2])
                logger.info(
                    f"JobDiva Talent Search: 0 rows for {len(must_terms)}-term AND, "
                    f"relaxing to {body['skills']!r}"
                )
                rows = await _post(body)

        title_clean = str(title or "").strip()
        if title_clean and getattr(_sc, "JOBDIVA_TALENT_TITLE_PULL_ENABLED", True):
            title_body = dict(base_body)
            title_body["titleSearch"] = title_clean
            title_rows = await _post(title_body)
            if title_rows:
                seen_ids = {
                    str(get_field(r, ["candidateId", "CANDIDATEID", "id", "ID"]) or "")
                    for r in rows
                }
                added = 0
                for r in title_rows:
                    cid = str(get_field(r, ["candidateId", "CANDIDATEID", "id", "ID"]) or "")
                    if not cid or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    rows.append(r)
                    added += 1
                logger.debug(
                    f"JobDiva Talent Search titleSearch pull: +{added} new rows "
                    f"({len(title_rows)} returned)"
                )

        if not must_terms and not title_clean:
            logger.warning(
                "JobDiva Talent Search: no usable skill terms or title — skipping "
                "(an empty search definition returns the unfiltered dump)"
            )
        return rows

    def _merge_detail_into_candidate(
        self,
        candidate: Dict[str, Any],
        detail: Dict[str, Any],
        counters: Dict[str, int],
    ) -> None:
        """Merge CandidatesDetail fields into an existing TalentSearch record.

        Detail values win when the existing field is empty/missing. Adds
        `address1` and `linkedin_url` (which TalentSearch doesn't return).
        Updates `counters` so the caller can log how much detail actually
        contributed.
        """
        def take(detail_keys: List[str]) -> str:
            value = get_field(detail, detail_keys)
            return str(value).strip() if value else ""

        if not candidate.get("email"):
            v = _get_candidate_email(detail)
            if v:
                candidate["email"] = v
                counters["email"] = counters.get("email", 0) + 1

        # Upgrade-only: fill an empty phone, or upgrade to a typed mobile from
        # the détail record, but never downgrade an existing (possibly-mobile)
        # number to a home/work one.
        chosen_phone = _select_better_phone(candidate.get("phone"), detail)
        if chosen_phone and chosen_phone != (candidate.get("phone") or "").strip():
            candidate["phone"] = chosen_phone
            counters["phone"] = counters.get("phone", 0) + 1

        addr = take(["address1", "ADDRESS1", "address", "ADDRESS"])
        if addr:
            candidate["address1"] = addr
            counters["address1"] = counters.get("address1", 0) + 1

        # Zip: detail wins (same rationale as city/state below) — feeds the
        # offline zip-radius match in unified_candidate_search.
        detail_zip = take(["zipcode", "ZIPCODE", "zip", "ZIP", "postalCode", "POSTALCODE", "postal_code"])
        if detail_zip and detail_zip != candidate.get("zipcode"):
            candidate["zipcode"] = detail_zip
            counters["zipcode"] = counters.get("zipcode", 0) + 1

        linkedin = take(["linkedinUrl", "LINKEDINURL", "linkedin", "LINKEDIN", "linkedIn", "LINKEDIN_URL"])
        if linkedin:
            candidate["linkedin_url"] = linkedin
            counters["linkedin"] = counters.get("linkedin", 0) + 1

        # Resume fallback: if TalentSearch left resume_text empty but detail
        # has it (or has a resumeId we hadn't seen), populate.
        if not (candidate.get("resume_text") or "").strip():
            detail_resume = self._extract_resume_text(detail)
            if detail_resume:
                candidate["resume_text"] = detail_resume
                if not candidate.get("abstract"):
                    candidate["abstract"] = detail_resume[:240].replace("\n", " ").strip()
                counters["resume"] = counters.get("resume", 0) + 1
        if not candidate.get("resume_id"):
            rid = take(["resumeId", "RESUMEID", "resume_id"])
            if rid:
                candidate["resume_id"] = rid

        # CandidatesDetail does not return a current job title, but
        # PROFESSION_SPECIALTY is the closest proxy and is often populated.
        # Use it only as a fallback when title is empty so the skill scorer
        # has something to match against.
        if not (candidate.get("title") or "").strip():
            specialty = take(["PROFESSION_SPECIALTY", "professionSpecialty"])
            if specialty:
                candidate["title"] = specialty

        # City/state can be more accurate in detail (TalentSearch sometimes
        # returns work-location vs candidate-location). When the detail
        # endpoint has a value, it wins — TalentSearch's value is the one
        # that diverges from what JobDiva shows on the candidate profile.
        detail_city = take(["city", "CITY", "locationCity", "LOCATIONCITY"])
        detail_state = take(["state", "STATE", "locationState", "LOCATIONSTATE"])
        city_changed = False
        state_changed = False
        if detail_city and detail_city != candidate.get("city"):
            candidate["city"] = detail_city
            city_changed = True
        if detail_state and detail_state != candidate.get("state"):
            candidate["state"] = detail_state
            state_changed = True
        if city_changed or state_changed or not candidate.get("location"):
            parts = [candidate.get("city", ""), candidate.get("state", "")]
            candidate["location"] = ", ".join([p for p in parts if p]).strip()
        if city_changed or state_changed:
            counters["location"] = counters.get("location", 0) + 1

        emp_status = take([
            "EMPLOYEESTATUS",
            "employeeStatus",
            "CURRENTEMPLOYEE",
            "currentEmployee",
            "ASSIGNMENTSTATUS",
            "assignmentStatus",
        ])
        if emp_status and not candidate.get("employee_status"):
            candidate["employee_status"] = emp_status

        avail_val = take(["AVAILABLE", "available", "STATUS", "status"])
        if avail_val and not candidate.get("available"):
            candidate["available"] = avail_val
            candidate["availability_status"] = avail_val

        quals = detail.get("qualifications") or detail.get("QUALIFICATIONS") or []
        if isinstance(quals, list) and quals and not candidate.get("qualifications"):
            candidate["qualifications"] = quals
            for q in quals:
                if isinstance(q, dict):
                    qval = str(q.get("qualificationValue") or q.get("value") or "").strip()
                    if "current employee" in qval.lower():
                        candidate["employee_status"] = "Current Employee"

    def _build_talent_boolean(
        self,
        skills: List[Any],
        location: str,
        zip_code: str = "",
        within_miles: Optional[int] = None,
    ) -> str:
        terms = []
        excludes = []
        geo_clause = ""
        for skill in skills or []:
            name = skill.get("value") if isinstance(skill, dict) else str(skill)
            match_type = skill.get("match_type", "must") if isinstance(skill, dict) else "must"
            if not name:
                continue
            term = f'"{str(name).strip()}"'
            if match_type == "exclude":
                excludes.append(term)
            else:
                terms.append(term)
        if location and location.strip():
            from core import sourcing_config as _sc
            if (
                getattr(_sc, "JOBDIVA_BOOLEAN_ZIP_DIALECT_ENABLED", False)
                and zip_code and str(zip_code).strip().isdigit()
            ):
                # Native geo dialect instead of a location keyword — the
                # quoted form only matches resumes containing the literal
                # string (probe-gated, same flag as the translator rewrite).
                miles = max(1, min(100, int(within_miles or 25)))
                geo_clause = f"Within {miles} miles of {str(zip_code).strip()}"
            else:
                terms.append(f'"{location.strip()}"')
        search_value = " AND ".join(terms) if terms else "*"
        if excludes:
            search_value = f"{search_value} NOT ({' OR '.join(excludes)})"
        if geo_clause:
            search_value = f"{search_value} {geo_clause}"
        return search_value

    def _calculate_match_score(self, candidate: Dict[str, Any], required_skills: List[Any] = None) -> int:
        """Calculate a real match score based on candidate skills vs job requirements."""
        if not required_skills:
            # Base score for candidates without specific requirements
            base_score = 65
            
            # Boost based on available data quality
            title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
            email = get_field(candidate, ["email", "EMAIL"]) or ""
            
            # Title quality scoring
            if any(word in title.lower() for word in ["senior", "lead", "principal", "architect"]):
                base_score += 10
            elif any(word in title.lower() for word in ["junior", "entry", "intern"]):
                base_score -= 5
                
            # Contact completeness
            if email and "@" in email:
                base_score += 5
                
            return min(base_score, 85)  # Cap at 85% without specific matching
        
        # Calculate actual skill matching
        candidate_skills = self._extract_candidate_skills(candidate)
        candidate_title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
        
        if not candidate_skills and not candidate_title:
            return 60  # Minimum score for candidates with no skill data
            
        matched_skills = 0
        total_required = len(required_skills)
        
        if total_required == 0:
            return 70  # Default when no requirements specified
            
        logger.info(f"🎯 Matching {len(candidate_skills)} candidate skills against {total_required} requirements")
        
        for req_skill in required_skills:
            skill_name = req_skill.get("value", "").lower() if isinstance(req_skill, dict) else str(req_skill).lower()
            
            # Check against candidate skills with improved matching
            skill_match = False
            for candidate_skill in candidate_skills:
                candidate_skill_lower = candidate_skill.lower()
                
                # Exact match
                if skill_name == candidate_skill_lower:
                    skill_match = True
                    break
                    
                # Partial match (either direction)
                elif (skill_name in candidate_skill_lower or 
                      candidate_skill_lower in skill_name):
                    skill_match = True
                    break
                    
                # Technology family matching (e.g., "react" matches "reactjs")
                elif self._are_similar_skills(skill_name, candidate_skill_lower):
                    skill_match = True
                    break
                    
            # Check against candidate title if no skill match
            if not skill_match and skill_name in candidate_title.lower():
                skill_match = True
                
            if skill_match:
                matched_skills += 1
                
        # Calculate base percentage 
        match_percentage = (matched_skills / total_required) * 100 if total_required > 0 else 70
        
        # Apply experience and seniority bonuses
        exp_years = self._extract_experience_years(candidate)
        if exp_years >= 10:
            match_percentage += 15  # Senior bonus
        elif exp_years >= 5:
            match_percentage += 10  # Mid-level bonus
        elif exp_years >= 2:
            match_percentage += 5   # Junior+ bonus
            
        # Skill depth bonus (more skills = better match potential)
        if len(candidate_skills) >= 8:
            match_percentage += 5
        elif len(candidate_skills) >= 5:
            match_percentage += 3
            
        final_score = max(45, min(95, int(match_percentage)))
        logger.info(f"📊 Final match score: {matched_skills}/{total_required} skills = {final_score}%")
        
        # Ensure reasonable bounds
        return final_score
    
    def _extract_candidate_skills(self, candidate: Dict[str, Any]) -> List[str]:
        """Extract skills from candidate data without using the Azure agent."""
        skills = []

        # Look for skills in various fields
        skill_fields = ["skills", "skillList", "technologies", "expertise", "summary"]
        for field in skill_fields:
            skill_data = get_field(candidate, [field])
            if skill_data:
                if isinstance(skill_data, str):
                    # Parse comma-separated or space-separated skills
                    potential_skills = [s.strip() for s in skill_data.replace(",", " ").split() if len(s.strip()) > 2]
                    skills.extend(potential_skills[:10])  # Limit to 10
                elif isinstance(skill_data, list):
                    skills.extend([str(s) for s in skill_data[:10]])
        
        # If no skills found from resume, try to infer basic skills from title and other fields
        if not skills:
            title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
            title_lower = title.lower()
            
            # Generate basic skills based on common job titles - conservative approach
            if "java" in title_lower and "developer" in title_lower:
                skills = ["Java", "Software Development"]
            elif "python" in title_lower:
                skills = ["Python", "Software Development"]  
            elif "react" in title_lower or "frontend" in title_lower:
                skills = ["JavaScript", "Frontend Development"]
            elif "data analyst" in title_lower or "data science" in title_lower:
                skills = ["Data Analysis", "SQL"]
            elif "qa" in title_lower or "test" in title_lower:
                skills = ["Testing", "Quality Assurance"]
            elif any(word in title_lower for word in ["accountant", "accounting", "payable", "receivable"]):
                skills = ["Accounting", "Financial Analysis"]
            else:
                # Very basic skills for unknown roles
                skills = ["Communication", "Problem Solving"]
        
        # Remove duplicates and limit - return empty list if no meaningful skills found  
        final_skills = list(set(skills))[:8]
        if len(final_skills) == 2 and set(final_skills) == {"Communication", "Problem Solving"}:
            return []  # Don't return generic skills - better to show empty
        
        return final_skills
    
    def _are_similar_skills(self, skill1: str, skill2: str) -> bool:
        """Check if two skills are similar (e.g., react vs reactjs, python vs python3)."""
        # Remove common suffixes/prefixes
        normalize = lambda s: re.sub(r'(\.js|js|\.py|py|\d+|[^\w])', '', s.lower())
        
        norm1, norm2 = normalize(skill1), normalize(skill2)
        
        # Check if normalized versions match
        if norm1 == norm2:
            return True
            
        # Check common technology aliases
        aliases = {
            'javascript': ['js', 'ecmascript'],
            'typescript': ['ts'],
            'python': ['py'],
            'react': ['reactjs'],
            'vue': ['vuejs'],
            'node': ['nodejs'],
            'sql': ['mysql', 'postgresql', 'postgres'],
            'aws': ['amazon web services'],
            'gcp': ['google cloud'],
            'azure': ['microsoft azure']
        }
        
        for base, alias_list in aliases.items():
            if ((norm1 == base and norm2 in alias_list) or 
                (norm2 == base and norm1 in alias_list)):
                return True
                
        return False

    def _extract_experience_years(self, candidate: Dict[str, Any]) -> int:
        """Extract years of experience from candidate data."""
        # Look for experience fields
        exp_fields = ["experience", "yearsExperience", "totalExperience", "workExperience", "experienceYears"]
        for field in exp_fields:
            exp_data = get_field(candidate, [field])
            if exp_data and isinstance(exp_data, (int, float)) and exp_data > 0:
                return int(exp_data)
        
        # Try to extract from text fields
        title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
        resume_text = get_field(candidate, ["resume", "resumeText", "summary"]) or ""
        
        # Look for patterns like "5+ years", "10 years experience", etc.
        import re
        text_to_search = f"{title} {resume_text}".lower()
        
        # Pattern matching for experience
        patterns = [
            r'(\d+)\+?\s*years?\s*(?:of\s*)?(?:experience|exp)',
            r'(\d+)\+?\s*yrs?\s*(?:of\s*)?(?:experience|exp)',
            r'over\s*(\d+)\s*years?',
            r'more\s*than\s*(\d+)\s*years?'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text_to_search)
            if matches:
                try:
                    years = int(matches[0])
                    if 0 <= years <= 50:  # Reasonable bounds
                        return years
                except ValueError:
                    continue
        
        # Infer from title seniority (more conservative estimates)
        title_lower = title.lower()
        if "senior" in title_lower or "sr" in title_lower:
            return 7  # Senior typically means 5-10 years
        elif "lead" in title_lower or "principal" in title_lower:
            return 10  # Lead/Principal typically means 8-15 years
        elif "architect" in title_lower or "manager" in title_lower:
            return 12  # Architect/Manager typically means 10+ years
        elif "junior" in title_lower or "jr" in title_lower:
            return 2   # Junior typically means 1-3 years
        elif "entry" in title_lower or "intern" in title_lower:
            return 1   # Entry level
        else:
            return 4   # Default mid-level experience

    def _extract_resume_text(self, candidate: Dict[str, Any]) -> str:
        """Extract resume/summary text from candidate data. Returns empty string if no resume found."""
        # Look for resume text in various fields
        resume_fields = ["resume", "resumeText", "summary", "profile", "description", "bio", "overview"]
        
        for field in resume_fields:
            resume_data = get_field(candidate, [field])
            if resume_data and isinstance(resume_data, str) and len(resume_data.strip()) > 20:
                # Clean HTML tags and return formatted text
                clean_text = re.sub(r'<[^>]+>', '', resume_data)
                clean_text = re.sub(r'\s+', ' ', clean_text)  # Normalize whitespace
                return clean_text.strip()
        
        # Return empty string if no resume found - no fallback generation
        return ""

    async def _fetch_candidate_details_batch(
        self,
        token: str,
        candidate_ids: List[str],
        chunk_size: int = 100,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Batch-fetch CandidatesDetail records and return them keyed by ID.

        Used by `_search_talent_pool` to enrich Talent Search results with
        fields the search payload doesn't reliably populate (address1,
        linkedinUrl, full email/phone). Chunks are issued with bounded
        concurrency (CANDIDATES_DETAIL_CONCURRENCY) and retried on 429/5xx
        with backoff so JobDiva's rate limiter doesn't silently drop records.
        """
        ids = [str(cid).strip() for cid in (candidate_ids or []) if cid and str(cid).strip()]
        if not ids:
            return {}

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        endpoint = f"{self.api_url}/apiv2/bi/CandidatesDetail"
        chunks = [ids[i:i + chunk_size] for i in range(0, len(ids), chunk_size)]

        # Bound how many chunks hit JobDiva at once and retry 429/5xx with
        # backoff. JobDiva rate-limits bursts of concurrent CandidatesDetail
        # requests (observed: 3 of 4 concurrent chunks 429'd), and the old
        # code dropped those records with no retry. See sourcing_config.
        from core import sourcing_config as _sc_det
        conc = max(1, int(getattr(_sc_det, "CANDIDATES_DETAIL_CONCURRENCY", 1)))
        backoffs = list(getattr(_sc_det, "CANDIDATES_DETAIL_RETRY_BACKOFF_S", [2.0, 5.0, 10.0, 20.0]))
        chunk_delay = float(getattr(_sc_det, "CANDIDATES_DETAIL_CHUNK_DELAY_S", 1.5))
        sem = asyncio.Semaphore(conc)

        async def _fetch_chunk(chunk: List[str], idx: int = 0) -> List[Dict[str, Any]]:
            for attempt in range(len(backoffs) + 1):
                try:
                    async with sem:
                        _c_t0 = time.perf_counter()
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            response = await client.get(
                                endpoint,
                                params={"candidateIds": chunk},
                                headers=headers,
                            )
                        _c_ms = (time.perf_counter() - _c_t0) * 1000.0
                        # Pace requests while still holding the slot so the
                        # next chunk can't burst past JobDiva's rate limiter.
                        if chunk_delay > 0:
                            await asyncio.sleep(chunk_delay)
                    try:
                        _c_bytes = len(response.content)
                    except Exception:
                        _c_bytes = 0
                    logger.info(
                        "CandidatesDetail chunk %d: %d ids -> HTTP %d in %.0fms "
                        "(setup+http), %d bytes (attempt %d/%d)",
                        idx, len(chunk), response.status_code, _c_ms, _c_bytes,
                        attempt + 1, len(backoffs) + 1,
                    )
                    if response.status_code == 200:
                        data = response.json()
                        if isinstance(data, dict):
                            payload = data.get("data") or []
                        else:
                            payload = data or []
                        if isinstance(payload, dict):
                            payload = [payload]
                        return list(payload)
                    # Retry rate-limit / server errors; give up on other 4xx.
                    if (response.status_code == 429 or response.status_code >= 500) and attempt < len(backoffs):
                        await asyncio.sleep(backoffs[attempt])
                        continue
                    logger.warning(
                        f"CandidatesDetail chunk {idx} failed: {response.status_code} - "
                        f"{response.text[:200]}"
                    )
                    return []
                except Exception as e:
                    if attempt < len(backoffs):
                        await asyncio.sleep(backoffs[attempt])
                        continue
                    logger.warning(f"CandidatesDetail chunk {idx} error: {e}")
                    return []
            return []

        results: Dict[str, Dict[str, Any]] = {}
        _det_t0 = time.perf_counter()
        chunked = await asyncio.gather(*[_fetch_chunk(chunk, i) for i, chunk in enumerate(chunks)])
        _det_ms = (time.perf_counter() - _det_t0) * 1000.0
        for batch in chunked:
            for record in batch:
                if not isinstance(record, dict):
                    continue
                cid = get_field(record, ["candidateId", "CANDIDATEID", "id", "ID"])
                if cid is None:
                    continue
                results[str(cid)] = record
        logger.info(
            "CandidatesDetail TIMING: ids=%d chunks=%d (chunk_size=%d, max_concurrency=%d) "
            "matched=%d total_ms=%.0f",
            len(ids), len(chunks), chunk_size, conc, len(results), _det_ms,
        )
        return results

    async def _fetch_candidate_notes_action_types_batch(
        self,
        token: str,
        candidate_ids: List[str],
        chunk_size: int = 50,
    ) -> Dict[str, List[str]]:
        """Fetch CandidateNotesListDetail for candidates and extract their ACTIONTYPEs."""
        ids = [str(cid).strip() for cid in (candidate_ids or []) if cid and str(cid).strip()]
        if not ids:
            return {}

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        endpoint = f"{self.api_url}/apiv2/bi/CandidateNotesListDetail"
        chunks = [ids[i:i + chunk_size] for i in range(0, len(ids), chunk_size)]
        results: Dict[str, List[str]] = {}

        for chunk in chunks:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(
                        endpoint,
                        params={"candidateIds": chunk},
                        headers=headers,
                    )
                if response.status_code == 200:
                    data = response.json()
                    payload = data.get("data") if isinstance(data, dict) else data
                    if isinstance(payload, list):
                        for note_row in payload:
                            if not isinstance(note_row, dict): continue
                            cid = str(note_row.get("CONTACTID") or note_row.get("CANDIDATEID") or "")
                            action_type = str(note_row.get("ACTIONTYPE") or "").strip()
                            if cid and action_type:
                                if cid not in results:
                                    results[cid] = []
                                results[cid].append(action_type)
            except Exception as e:
                logger.warning(f"CandidateNotesListDetail fetch failed: {e}")
        return results


    async def _fetch_resume_text_batch(
        self,
        token: str,
        candidate_ids: List[str],
        concurrency: int = 8,
    ) -> Dict[str, str]:
        """Concurrently fetch resume text for a list of candidate IDs.

        Used by `_search_talent_pool` to enrich the survivors of the state
        filter with actual resume content — TalentSearch on its own returns
        empty `resume_text` for most candidates, which makes the skill scorer
        useless. Reuses the existing `_get_resume_detail_with_id` chain so we
        respect resume-selection logic (most-recent preferred) and the
        ResumesTextDetail / fallback endpoints.

        Returns `{candidate_id: resume_text}` keyed by ID. Failures per
        candidate produce an empty string entry; failures are logged in
        aggregate.
        """
        ids = [str(cid).strip() for cid in (candidate_ids or []) if cid and str(cid).strip()]
        if not ids:
            return {}

        semaphore = asyncio.Semaphore(max(1, concurrency))
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        out: Dict[str, str] = {}
        failures = 0

        async def _one(cid: str) -> None:
            nonlocal failures
            async with semaphore:
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        result = await self._get_resume_detail_with_id(cid, client, headers)
                    out[cid] = (result or {}).get("resume_text", "") or ""
                except Exception as e:
                    failures += 1
                    out[cid] = ""
                    logger.debug(f"_fetch_resume_text_batch: {cid} failed: {e}")

        await asyncio.gather(*[_one(cid) for cid in ids])
        if failures:
            logger.info(
                f"_fetch_resume_text_batch: fetched {len(ids) - failures}/{len(ids)} "
                f"resumes ({failures} failed)"
            )
        return out

    async def get_candidate_details(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed candidate information using /apiv2/bi/CandidatesDetail endpoint."""
        token = await self.authenticate()
        if not token:
            logger.warning(f"JobDiva authentication failed for candidate {candidate_id}")
            return None
        result = await self._fetch_candidate_details_batch(token, [candidate_id])
        return result.get(str(candidate_id))
    
    async def get_candidate_resumes(self, candidate_id: str) -> Optional[List[Dict[str, Any]]]:
        """Get all resumes for a candidate using /apiv2/bi/CandidatesResumesDetail endpoint."""
        token = await self.authenticate()
        if not token:
            logger.warning(f"JobDiva authentication failed for candidate {candidate_id}")
            return None
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        endpoint = f"{self.api_url}/apiv2/bi/CandidatesResumesDetail"
        params = {"candidateIds": [candidate_id]}
        
        try:
            logger.debug(f"Fetching candidate resumes for {candidate_id}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(endpoint, params=params, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if isinstance(data, dict) and "data" in data:
                        resumes = data["data"]
                        if resumes:
                            resume_count = len(resumes) if isinstance(resumes, list) else 1
                            logger.debug(f"Found {resume_count} resume(s) for candidate {candidate_id}")
                            return resumes if isinstance(resumes, list) else [resumes]
                    
        except Exception as e:
            logger.debug(f"Error fetching candidate resumes for {candidate_id}: {e}")
        
        return None

    async def get_candidate_resume(
        self,
        candidate_id: str,
        resume_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch full candidate resume/details by ID using JobDiva API v2 endpoints."""
        logger.debug(f"Getting resume for candidate {candidate_id}")
        
        token = await self.authenticate()
        if not token:
            logger.warning(f"JobDiva authentication failed for candidate {candidate_id}")
            return None
        
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        # Try the working resume fetching logic with cascading fallback
        resume_text = ""
        selected_resume_id = resume_id
        candidate_info = {}
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: Try to get candidate details
                details_url = f"{self.api_url}/apiv2/bi/CandidatesDetail"
                details_resp = await client.get(details_url, params={"candidateIds": [candidate_id]}, headers=headers)
                
                if details_resp.status_code == 200:
                    details_data = details_resp.json()
                    if isinstance(details_data, dict) and "data" in details_data:
                        candidates = details_data["data"]
                        if candidates and len(candidates) > 0:
                            candidate_info = candidates[0] if isinstance(candidates, list) else candidates
                            logger.debug(f"Got candidate details for {candidate_id}")
                
                if resume_id:
                    # Applicant flow: JobDiva already told us the exact resume for
                    # this applicant. Fetch that resume text directly; do not
                    # re-select from the candidate's full resume history.
                    resume_text = await self._get_resume_text_by_id(str(resume_id), client, headers)
                else:
                    resume_result = await self._get_resume_detail_with_id(
                        candidate_id,
                        client,
                        headers,
                    )
                    resume_text = resume_result.get("resume_text", "")
                    selected_resume_id = resume_result.get("resume_id") or selected_resume_id
                                
        except Exception as e:
            logger.warning(f"Error fetching candidate resume for {candidate_id}: {e}")
        
        # If we didn't get candidate info, create basic info from candidate_id
        if not candidate_info:
            candidate_info = {
                "candidateId": candidate_id,
                "firstName": "Unknown",
                "lastName": "Candidate"
            }
        
        # Add resume text to candidate info
        candidate_info["resume_text"] = resume_text or "Resume content unavailable"
        candidate_info["resume_id"] = selected_resume_id
        candidate_info["resume_count"] = 1 if resume_text else 0
        
        return self._format_candidate_resume(candidate_info)
    
    def _format_candidate_resume(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Format candidate data for resume display."""
        # Extract basic info using multiple possible field names
        candidate_id = get_field(candidate, ["candidateId", "id", "ID", "CANDIDATEID"]) or ""
        first_name = get_field(candidate, ["firstName", "FIRSTNAME", "firstname"]) or ""
        last_name = get_field(candidate, ["lastName", "LASTNAME", "lastname"]) or ""
        full_name = f"{first_name} {last_name}".strip() or candidate.get("name", "") or "Professional Candidate"
        
        # Extract resume text - could be in different fields
        resume_text = get_field(candidate, ["resume_text", "resumeText", "RESUMETEXT", "text", "content"]) or ""
        
        # If no resume text found, try to extract from resume data structure
        if not resume_text:
            resume_text = self._extract_resume_text(candidate)
        
        return {
            "id": str(candidate_id),
            "name": full_name,
            "firstName": first_name,
            "lastName": last_name,
            "email": _get_candidate_email(candidate) or "Available upon request",
            "phone": _get_candidate_phone(candidate) or "Available upon request",
            "title": get_field(candidate, ["title", "TITLE", "currentTitle", "jobTitle"]) or "",
            "location": get_field(candidate, ["location", "city", "CITY"]) or "",
            "work_city": get_field(candidate, ["work_city", "workCity", "WORKCITY"]) or "",
            "work_state": get_field(candidate, ["work_state", "workState", "WORKSTATE"]) or "",
            "work_location": get_field(candidate, ["work_location"]) or "",
            "text": resume_text,  # Main resume text field
            "resume_text": resume_text,  # Backup field name
            "resume_id": get_field(candidate, ["resume_id", "resumeId", "RESUMEID"]),
            "skills": self._extract_candidate_skills(candidate),
            "experience": get_field(candidate, ["experience", "EXPERIENCE", "experienceYears"]) or "",
            "education": get_field(candidate, ["education", "EDUCATION"]) or "",
            "resume_count": candidate.get("resume_count", 1),
            "source": "JobDiva"
        }
    
    async def get_candidate_by_id(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a specific candidate by ID from JobDiva."""
        logger.info(f"Fetching Candidate ID: {candidate_id}")
        token = await self.authenticate()
        if not token: return None

        url = f"{self.api_url}/apiv2/jobdiva/getCandidateById"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"candidateId": candidate_id}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.warning(f"❌ Failed to fetch candidate {candidate_id}: {response.status_code}")
                    return None
        except Exception as e:
            logger.error(f"❌ Exception fetching candidate {candidate_id}: {e}")
            return None

    async def get_job_by_id(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a specific job by ID from JobDiva, including AI UDFs."""
        # Versioned refs (26-06182-v2) are local clones that share the original
        # JobDiva job — strip the -vN suffix so the external SearchJob lookup
        # uses the root ref and doesn't 404 / trip the strict ref-match guard.
        # The caller keeps the versioned ref for any local DB identity it needs.
        job_id = strip_job_version_suffix(job_id)
        logger.info(f"Fetching Job ID: {job_id}")
        token = await self.authenticate()
        if not token: return None

        url = f"{self.api_url}/apiv2/jobdiva/SearchJob"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        # NEW: Prefer Reference Number search even if numeric ID was provided
        # (JobDiva SearchJob API is more reliable with ref numbers than legacy numeric IDs)
        is_ref = "-" in job_id
        search_id = job_id
        
        if not is_ref:
            local_job = self.get_locally_monitored_job(job_id)
            if local_job and local_job.get("jobdiva_id"):
                search_id = local_job.get("jobdiva_id")
                is_ref = True
                logger.info(f"🔄 ID-Resolution: Using Reference {search_id} instead of numeric ID {job_id} for better reliability")

        if is_ref:
            payload = {"jobdivaref": search_id, "maxReturned": 1}
        else:
            safe_id = "".join(filter(str.isdigit, job_id))
            if not safe_id: return None
            payload = {"jobOrderId": int(safe_id), "maxReturned": 1}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code != 200: return None
                data = response.json()
                jobs = data if isinstance(data, list) else data.get("data", [])
                if not jobs: return None
                j = jobs[0]
            
                # Strict Matching: JobDiva sometimes returns arbitrary jobs for invalid inputs like '1'
                j_id = str(get_field(j, ["id", "jobId"]) or "")
                j_ref = str(get_field(j, ["reference #", "jobdivaref", "ref", "jobdivano"]) or "")
                
                if is_ref:
                    if search_id.lower() != j_ref.lower():
                        logger.warning(f"Bogus JobDiva response: requested ref {search_id}, got ref {j_ref}")
                        return None
                else:
                    if safe_id != j_id:
                        # NEW: Relaxed ID matching for Aliases (ID 31920032 vs 9165998)
                        # Check local DB for expected reference first
                        local_job = self.get_locally_monitored_job(job_id)
                        expected_ref = local_job.get("jobdiva_id")
                        
                        if expected_ref and str(expected_ref).lower() == j_ref.lower():
                            logger.info(f"✅ Aliased ID Accepted: requested ID {safe_id}, got ID {j_id} (Ref {j_ref} matches local DB)")
                        else:
                            logger.warning(f"Bogus JobDiva response: requested ID {safe_id}, got ID {j_id}. Ref '{j_ref}' did not match expected '{expected_ref}'")
                            return None
                        
                # ----------------------------------------------------
                # CRITICAL: JobDiva v2 SearchJob endpoint randomly drops fields like MAXALLOWEDSUBMITTALS
                # We supplement it here using the /apiv2/bi/JobDetail BI endpoint which retains them.
                # ----------------------------------------------------
                detail_url = f"{self.api_url}/apiv2/bi/JobDetail"
                detail_params = {"jobdivaref": j_ref} if j_ref else {"jobId": j_id}
                try:
                    det_resp = await client.get(detail_url, params=detail_params, headers=headers)
                    if det_resp.status_code == 200:
                        det_data = det_resp.json()
                        det_list = det_data.get("data", []) if isinstance(det_data, dict) else det_data
                        if det_list and len(det_list) > 0:
                            d = det_list[0]
                            max_sub = d.get("MAXALLOWEDSUBMITTALS")
                            if max_sub:
                                j["maxAllowedSubmittals"] = max_sub
                            
                            # Add rock-solid BI Customer/Company name extraction
                            # We search for every possible variation found across different JobDiva setups
                            bi_keys = [
                                "CUSTOMERNAME", "COMPANYNAME", "CUSTOMER", "COMPANY", 
                                "CLIENTNAME", "CLIENT_NAME", "CLIENT", "NAME", "COMPANY_FULL_NAME"
                            ]
                            for ckey in bi_keys:
                                if d.get(ckey):
                                    j["customer_bi"] = d.get(ckey)
                                    logger.info(f"Found customer '{j['customer_bi']}' in BI field '{ckey}'")
                                    break

                            # Add robust BI Date and Status Extraction
                            if d.get("JOBSTATUS"):
                                j["JOBSTATUS_BI"] = d.get("JOBSTATUS")
                            if d.get("DATEISSUED"):
                                j["DATEISSUED_BI"] = d.get("DATEISSUED")
                            if d.get("STARTDATE"):
                                j["STARTDATE_BI"] = d.get("STARTDATE")
                except Exception as e:
                    logger.warning(f"Failed to fetch JobDetail supplemental data: {e}")
                
                u_fields = j.get("user fields", {}) or {}
                ai_description = None
                job_notes = None
                salary_range_udf = None
                issued_date_udf = None
                for k, v in u_fields.items():
                    k_low = k.lower()
                    # if "ai job description" in k_low: ai_description = v
                    # if "job notes" in k_low or k == "231": job_notes = v
                    if "salary range" in k_low or "pay range" in k_low or "pay rate" in k_low: salary_range_udf = v
                    if "issued date" in k_low or "posted date" in k_low or "date issued" in k_low or k_low == "issued" or k_low == "posted": issued_date_udf = v

                # Resolution Priority:
                # 1. BI Metadata (Most reliable)
                # 2. Standard API fields (company, customer, etc.)
                # 3. Regex parsing of description (Last resort)
                # 4. Local DB Restore (handled below)
                
                raw_customer = j.get("customer_bi") or get_field(j, ["customer", "company", "client", "customerName", "companyName", "clientName", "client_name"])
                
                description = format_job_description(get_field(j, ["job description", "description"]) or "")
                
                if not raw_customer or raw_customer.lower() in ["unknown", "unknown customer", ""]:
                    # Try parsing the first 500 characters of the description for common patterns
                    raw_customer = self._extract_customer_from_description(description)
                    if raw_customer:
                        logger.info(f"Extracted customer '{raw_customer}' from description text")

                customer_name = str(raw_customer or "").title() or "Unknown Customer"

                # ONLY restore full-length UDFs from local DB if JobDiva version looks truncated
                # and is NOT empty (which would mean it was cleared in JobDiva)
                local_data = self.get_locally_monitored_job(job_id)
                if local_data:
                    local_ai = local_data.get("ai_description")
                    # If JobDiva AI Description is not empty, but local is longer, assume truncation 
                    if local_ai and ai_description and len(str(ai_description)) > 3000 and len(str(local_ai)) > len(str(ai_description)):
                        ai_description = local_ai
                        logger.info(f"Restored full ai_description from local DB for {job_id}")
                    
                    local_notes = local_data.get("recruiter_notes")
                    if local_notes and job_notes and len(str(job_notes)) > 1000 and len(str(local_notes)) > len(str(job_notes)):
                        job_notes = local_notes
                        logger.info(f"Restored full recruiter_notes from local DB for {job_id}")
                    
                    # NEW: Restore customer_name from local DB if currently Unknown or Empty
                    local_customer = local_data.get("customer_name")
                    if local_customer and str(local_customer).lower() != "unknown" and (not customer_name or str(customer_name).lower() == "unknown" or customer_name == "Unknown Customer"):
                        customer_name = local_customer
                        logger.info(f"🔄 Self-Healed: Restored customer_name '{customer_name}' from local DB for {job_id}")

                # Advanced pay_rate logic: try to combine min and max if available for a range
                p_min = get_field(j, ["minpayrate", "min_pay_rate", "minimum_pay", "payRateMin", "minimum rate"])
                p_max = get_field(j, ["maxpayrate", "max_pay_rate", "maximum_pay", "payRateMax", "maximum rate"])
                
                # Format to ignore zeros
                if str(p_min) == "0" or str(p_min) == "0.0": p_min = None
                if str(p_max) == "0" or str(p_max) == "0.0": p_max = None
                
                # Determine rate unit suffix dynamically based on 'rate per' from JobDiva
                # Handles all JobDiva units: $/Hour, $/Day, $/Month, $/Year, INR/*, C$/*, MXN/*
                # JobDiva uses single-char codes: 'y'=year, 'h'=hour, 'd'=day, 'm'/'mo'=month
                # If no unit is provided, display rate as-is without any suffix
                rate_per = get_field(j, ["rate per", "rate_per", "PAYRATEPER", "payRatePer"])
                rate_unit = "" # No suffix by default
                
                if rate_per:
                    rate_per_str = str(rate_per).lower().strip()
                    # Exact match for JobDiva single-char codes first, then substring for full words
                    if rate_per_str in ["y"] or any(term in rate_per_str for term in ["year", "yearly", "annual", "annum", "/yr"]):
                        rate_unit = "/yr"
                    elif rate_per_str in ["mo", "m"] or any(term in rate_per_str for term in ["month", "monthly", "/mo"]):
                        rate_unit = "/mo"
                    elif rate_per_str in ["d"] or any(term in rate_per_str for term in ["day", "daily", "/day"]):
                        rate_unit = "/day"
                    elif rate_per_str in ["h"] or any(term in rate_per_str for term in ["hour", "hourly", "/hr", "/h"]):
                        rate_unit = "/h"


                if p_min and p_max:
                    p_range = f"${p_min} - ${p_max}{rate_unit}"
                elif p_max:
                    p_range = f"${p_max}{rate_unit}"
                elif p_min:
                    p_range = f"${p_min}{rate_unit}"
                else:
                    p_range = ""
                
                # Improved Location Type detection - Only use actual location fields, not employment fields
                loc_type_raw = get_field(j, ["location type", "location_type", "onsite_remote", "onsiteremote", "onsite remote", "onSiteRemote"]) or ""
                val_lower = str(loc_type_raw).lower().strip()
                
                loc_type = ""
                
                # 1. Look for explicit keywords in the raw location field
                if "remote" in val_lower:
                    loc_type = "Remote"
                elif "hybrid" in val_lower:
                    loc_type = "Hybrid"
                elif "onsite" in val_lower or "on-site" in val_lower:
                    loc_type = "Onsite"
                elif val_lower:
                    # 2. Check for employment type contamination if no explicit location word found
                    employment_terms = [
                        "direct placement", "contract", "full-time", "part-time", 
                        "w2", "1099", "c2c", "corp to corp", "open", "pending",
                        "temporary", "permanent", "temp to perm", "fulltime", "parttime"
                    ]
                    if not any(term in val_lower for term in employment_terms):
                        loc_type = str(loc_type_raw).strip()
                
                # 3. Prioritize Job Description over JobDiva API field
                # JobDiva API often incorrectly defaults to "Remote" when JD says Hybrid/Onsite.
                desc_lower = description.lower()
                
                has_hybrid = False
                # Only treat "hybrid" as a work-arrangement signal when it appears
                # near work-context words. Avoid false positives from tech JDs that
                # say "hybrid cloud", "hybrid architecture", "hybrid environment" etc.
                _hybrid_work_phrases = [
                    "hybrid role", "hybrid position", "hybrid work", "hybrid schedule",
                    "hybrid model", "hybrid arrangement", "hybrid option",
                    "hybrid setting", "hybrid basis", "hybrid format",
                    "hybrid working", "hybrid opportunity", "hybrid flexibility",
                ]
                _hybrid_tech_phrases = [
                    "hybrid cloud", "hybrid environment", "hybrid architecture",
                    "hybrid infrastructure", "hybrid network", "hybrid system",
                    "hybrid solution", "hybrid deployment", "hybrid setup",
                    "hybrid approach", "hybrid technology", "hybrid platform",
                    "hybrid data", "hybrid storage",
                ]
                if "hybrid" in desc_lower:
                    # Has a work-context phrase → definitely hybrid work arrangement
                    if any(phrase in desc_lower for phrase in _hybrid_work_phrases):
                        has_hybrid = True
                    # Only has tech phrases → NOT a work arrangement signal
                    elif any(phrase in desc_lower for phrase in _hybrid_tech_phrases):
                        has_hybrid = False
                    else:
                        # Ambiguous standalone "hybrid" mention — trust the API field
                        has_hybrid = ("hybrid" in val_lower)
                # Tighten onsite matching using regex with word boundaries to avoid false positives like "depending on site conditions"
                has_onsite = bool(re.search(r'\b(?:onsite|on-site|work\s+on\s+site|working\s+on\s+site|on\s+site\s+(?:work|role|position|basis|location|office|presence|environment|days|requirement|required|mandatory|essential|only))\b', desc_lower))

                # Check for "remote" but carefully exclude negative phrases using word-bounded regex.
                # e.g. "not a WFH/remote role", "not remote", "no remote", "non-remote"
                _remote_mention = bool(re.search(r'\bremote\b', desc_lower))
                _remote_negated = bool(re.search(r'\b(?:not|no|non|never)(?:-|\s+)(?:a\s+|an\s+)?(?:remote|wfh|work\s+from\s+home|(?:wfh/)?remote)\b', desc_lower))
                has_remote = _remote_mention and not _remote_negated
                
                # Determine what the API explicitly said
                api_loc = ""
                if "hybrid" in val_lower: api_loc = "Hybrid"
                elif "remote" in val_lower: api_loc = "Remote"
                elif "onsite" in val_lower or "on-site" in val_lower: api_loc = "Onsite"
                
                # If API and JD both agree on Onsite, trust it — even if "remote" appears
                # negatively in the JD (e.g. "This is not a WFH/remote role").
                if api_loc == "Onsite" and has_onsite and not has_hybrid:
                    loc_type = "Onsite"
                elif has_hybrid:
                    loc_type = "Hybrid"
                elif has_onsite and has_remote:
                    # Mentions both Onsite and Remote -> usually implies a Hybrid arrangement
                    loc_type = "Hybrid"
                elif has_onsite:
                    loc_type = "Onsite"
                elif has_remote:
                    loc_type = "Remote"
                elif _remote_negated and api_loc == "Remote":
                    # JD explicitly says "not remote" / "no WFH" but API says Remote.
                    # The JD overrides the API — the job is clearly NOT remote.
                    # Default to Onsite since the JD is denying remote without naming an alternative.
                    loc_type = "Onsite"
                else:
                    # JD is silent about location keywords, trust the API field
                    loc_type = api_loc
                        
                if not loc_type:
                    loc_type = "Onsite"
                
                result = {
                    "id": get_field(j, ["id", "jobId"]),
                    "jobdiva_id": get_field(j, ["jobdivano", "reference #", "refno", "jobdivaref", "ref"]),
                    "title": get_field(j, ["job title", "title"]),
                    "description": description,
                    "jobdiva_description": description, # Clarified for schema
                    "ai_description": ai_description if ai_description is not None else "",
                    "recruiter_notes": job_notes if job_notes is not None else "",
                    "customer_name": customer_name,
                    "job_status": j.get("JOBSTATUS_BI") or get_field(j, ["job status", "status"]) or "OPEN",
                    "status": j.get("JOBSTATUS_BI") or get_field(j, ["job status", "status"]) or "OPEN", # Database standard
                    "city": _clean_location_field(get_field(j, ["city", "jobCity", "locationCity", "worksitecity"])),
                    "state": _clean_location_field(get_field(j, ["state", "jobState", "locationState", "worksitestate", "province"])),
                    "zip_code": _clean_location_field(get_field(j, ["zip", "postalCode", "zipcode", "postalcode", "worksitezip", "worksitepostalcode"])),
                    "start_date": normalize_jobdiva_date(j.get("STARTDATE_BI") or get_field(j, ["start date", "startDate", "available", "startdate"]) or (local_data.get("start_date") if local_data else "")),
                    "issued_date": normalize_jobdiva_date(j.get("DATEISSUED_BI") or issued_date_udf or get_field(j, ["issued date", "issueddate", "issued_date", "issued"]) or (local_data.get("issued_date") if local_data else "")),
                    "posted_date": normalize_jobdiva_date(j.get("DATEISSUED_BI") or get_field(j, ["posted date", "date", "created date", "posted", "posteddate", "createtimestamp", "date_posted", "posted_at"]) or issued_date_udf or get_field(j, ["issued date", "issueddate", "issued_date", "issued"]) or extract_posted_date_from_text(description) or (local_data.get("posted_date") if local_data else "")) or get_fallback_posted_date(),
                    "location_type": loc_type,
                    "work_authorization": get_field(j, ["work_authorization", "visa", "legal status", "workauth", "work_auth", "work authorization"]) or (local_data.get("work_authorization") if local_data else ""),
                    
                    # Extract multiple recruiter emails from JobDiva API - store in job_configuration only
                    "recruiter_emails": extract_multiple_recruiter_emails(j),
                    
                    "pay_rate": salary_range_udf or p_range or get_field(j, ["pay rate", "salary range", "salary", "rate", "bill rate", "compensation", "billrate", "payrate"]) or extract_pay_rate_from_text(description) or (local_data.get("pay_rate") if local_data else ""),
                    "openings": get_field(j, ["openings", "maxReturned", "positions", "number of openings", "openpositions"]) or (local_data.get("openings") if local_data else ""),
                    "employment_type": normalize_employment_type(get_field(j, ["employment type", "jobType", "assignmentType"]) or (local_data.get("employment_type") if local_data else "")),
                    "required_degree": get_field(j, ["required degree", "required_degree", "criteria degree", "criteria_degree"]) or "",

                    # Extended JobDiva fields
                    "priority": str(get_field(j, ["priority", "jobPriority", "job priority"]) or (local_data.get("priority") if local_data else "") or ""),
                    "program_duration": str(get_field(j, ["duration", "program duration", "contract duration", "program_duration", "assignment duration", "assignmentDuration", "contractDuration"]) or (local_data.get("program_duration") if local_data else "") or ""),
                    "max_allowed_submittals": str(get_field(j, ["max submittals", "maxsubmittals", "max submissions", "maximum submittals", "max_allowed_submittals", "maxResumeSubmittal", "maxAllowedSubmittals"]) or (local_data.get("max_allowed_submittals") if local_data else "") or ""),
                }
                
                # Dynamic Duration Calculation if missing
                if not result.get("program_duration") or result.get("program_duration") == "None":
                    raw_end_date = get_field(j, ["end date", "endDate", "enddate"])
                    if raw_end_date:
                        end_date_str = normalize_jobdiva_date(raw_end_date)
                        calc_duration = calculate_date_duration(result.get("start_date", ""), end_date_str)
                        if calc_duration:
                            result["program_duration"] = calc_duration

                return result
        except Exception as e:
            logger.exception(f"❌ SearchJob Error for job_id {job_id}: {e}")
            return None


    async def get_enhanced_job_candidates(self, job_id: str) -> List[Dict[str, Any]]:
        """
        Enhanced candidate retrieval combining three JobDiva API endpoints:
        1. JobApplicantsDetail - Get job applicants
        2. CandidateDetail - Get candidate info  
        3. ResumeDetail - Get full resume text
        """
        token = await self.authenticate()
        if not token: 
            return []

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        enhanced_candidates = []

        try:
            # Resolve numeric ID if it's a reference number
            safe_id = job_id
            if "-" in job_id:
                logger.info(f"🔄 Resolving numeric ID for reference {job_id}")
                job_info = await self.get_job_by_id(job_id)
                if job_info:
                    # SearchJob returns job id in different fields sometimes
                    resolved_id = get_field(job_info, ["id", "jobId", "jobOrderID"])
                    if resolved_id:
                        safe_id = str(resolved_id)
                        logger.info(f"✅ Resolved {job_id} to internal numeric ID: {safe_id}")

            # Step 1: Get Job Applicants using JobApplicantsDetail
            applicants_url = f"{self.api_url}/apiv2/bi/JobApplicantsDetail"
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"🔍 Fetching job applicants for job_id: {safe_id}")
                
                applicants_response = await client.get(
                    applicants_url, 
                    params={"jobId": safe_id}, 
                    headers=headers
                )
                
                if applicants_response.status_code != 200:
                    logger.error(f"❌ JobApplicantsDetail failed: {applicants_response.status_code}")
                    return enhanced_candidates
                
                applicants_data = applicants_response.json()
                applicants = applicants_data.get("data", []) if isinstance(applicants_data, dict) else applicants_data

                logger.info(f"📋 Found {len(applicants)} job applicants")

                # Batch-fetch CandidatesDetail for all applicants in one go.
                # JobDiva's CandidatesDetail accepts up to 100 candidateIds per
                # call; the batch helper chunks internally if we exceed that.
                applicant_candidate_ids = [
                    str(a.get("CANDIDATEID") or a.get("candidateId"))
                    for a in applicants
                    if a.get("CANDIDATEID") or a.get("candidateId")
                ]
                detail_map = await self._fetch_candidate_details_batch(token, applicant_candidate_ids)

                # Step 2 & 3: For each applicant, look up batched detail and fetch resume
                for idx, applicant in enumerate(applicants, 1):
                    try:
                        candidate_id = applicant.get("CANDIDATEID") or applicant.get("candidateId")
                        resume_id = applicant.get("RESUMEID") or applicant.get("resumeId")

                        if not candidate_id:
                            continue

                        logger.debug(f"[{idx}/{len(applicants)}] Processing applicant {candidate_id}")

                        candidate_detail = detail_map.get(str(candidate_id), {})
                        
                        # Use the specific resume ID from applicant data when JobDiva provides it.
                        resume_text = ""
                        if resume_id:
                            resume_text = await self._get_resume_text_by_id(resume_id, client, headers)
                        else:
                            resume_result = await self._get_resume_detail_with_id(candidate_id, client, headers)
                            resume_text = resume_result.get("resume_text", "")
                            resume_id = resume_result.get("resume_id")
                        
                        # Combine all data
                        enhanced_candidate = self._format_enhanced_candidate(
                            applicant, candidate_detail, resume_text, "job_applicant"
                        )
                        
                        enhanced_candidates.append(enhanced_candidate)
                        
                    except Exception as e:
                        logger.warning(f"⚠️ Error processing applicant {candidate_id}: {e}")
                        continue

        except Exception as e:
            logger.error(f"❌ Error in get_enhanced_job_candidates: {e}")
        
        return enhanced_candidates

    async def get_candidate_profile_url(self, candidate_id: str) -> str:
        """
        Fetch a JobDiva candidate's profile URL on demand.

        JobDiva's Talent Search response does not include PROFILEURL, but the
        CandidatesDetail endpoint does (at least for tenants that publish it).
        Routers use this as a lightweight on-click enrichment so candidate names
        in Step 5 can hyperlink to the JobDiva profile without eagerly pulling
        details for every result.

        Returns an empty string if no URL can be resolved — callers should treat
        that as "render plain text, no link".
        """
        if not candidate_id:
            return ""

        try:
            detail = await self.get_candidate_details(str(candidate_id)) or {}
            profile_url = (
                get_field(detail, ["PROFILEURL", "profileUrl", "profile_url", "PROFILE_URL"])
                or ""
            )
            return str(profile_url).strip()
        except Exception as e:
            logger.debug(f"get_candidate_profile_url failed for {candidate_id}: {e}")
            return ""

    def _parse_jobdiva_datetime(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _resume_timestamp(self, resume: Dict[str, Any]) -> datetime:
        """Return the best sortable timestamp JobDiva gives us for a resume."""
        for key in ["DATEUPDATED", "DATECREATED", "DATELASTDOWNLOADED", "DATEFIRSTDOWNLOADED"]:
            parsed = self._parse_jobdiva_datetime(get_field(resume, [key, key.lower(), key.title()]))
            if parsed:
                return parsed
        return datetime.min

    def _resume_created_timestamp(self, resume: Dict[str, Any]) -> datetime:
        parsed = self._parse_jobdiva_datetime(
            get_field(resume, ["DATECREATED", "dateCreated", "datecreated"])
        )
        return parsed or datetime.min

    def _select_resume_record(
        self,
        resumes: List[Dict[str, Any]],
        preferred_resume_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Prefer the exact resume ID; otherwise choose the newest created resume."""
        if not resumes:
            return None

        if preferred_resume_id:
            preferred = str(preferred_resume_id).strip()
            for resume in resumes:
                resume_id = get_field(resume, ["RESUMEID", "resumeId", "ID", "resume_id"])
                if str(resume_id or "").strip() == preferred:
                    return resume

        def sort_key(resume: Dict[str, Any]):
            doc_id = get_field(resume, ["DOCID", "docId", "ID"]) or 0
            try:
                doc_id = int(doc_id)
            except Exception:
                doc_id = 0
            return (self._resume_created_timestamp(resume), self._resume_timestamp(resume), doc_id)

        return sorted(resumes, key=sort_key, reverse=True)[0]

    async def _get_resume_records(
        self,
        candidate_id: str,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> List[Dict[str, Any]]:
        """Get resume metadata records for a candidate using JobDiva's BI endpoint."""
        endpoint_attempts = [
            (f"{self.api_url}/apiv2/bi/CandidateResumesDetail", {"candidateId": candidate_id}),
            (f"{self.api_url}/apiv2/bi/CandidatesResumesDetail", {"candidateIds": [candidate_id]}),
        ]

        for url, params in endpoint_attempts:
            try:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code != 200:
                    logger.debug(f"{url.rsplit('/', 1)[-1]} returned {response.status_code} for {candidate_id}")
                    continue

                data = response.json()
                resumes = data.get("data", []) if isinstance(data, dict) else data
                if isinstance(resumes, dict):
                    resumes = [resumes]
                if resumes:
                    return resumes
            except Exception as e:
                logger.debug(f"Error fetching resume records for {candidate_id}: {e}")

        return []

    async def _get_resume_detail_with_id(
        self,
        candidate_id: str,
        client: httpx.AsyncClient,
        headers: dict,
        preferred_resume_id: str = None,
    ) -> Dict[str, str]:
        """Get full resume text and the selected resume ID."""
        try:
            logger.debug(f"📄 Fetching resume for candidate ID: {candidate_id}")

            # Step 1: Get all resume IDs for this candidate using CandidateResumesDetail.
            resumes = await self._get_resume_records(candidate_id, client, headers)
            if not resumes:
                logger.debug(f"No resumes found for candidate {candidate_id}")
                return {"resume_text": "", "resume_id": ""}
                
            logger.debug(f"Found {len(resumes)} resume(s) for candidate {candidate_id}")

            selected_resume = self._select_resume_record(resumes, preferred_resume_id)
            selected_resume_id = get_field(selected_resume or {}, ["RESUMEID", "resumeId", "ID", "resume_id"])
            if not selected_resume_id:
                return {"resume_text": "", "resume_id": ""}

            # Step 2: Get resume text using ResumesTextDetail (plural) endpoint.
            resume_text = await self._get_resume_text_by_id(str(selected_resume_id), client, headers)
            return {
                "resume_text": resume_text,
                "resume_id": str(selected_resume_id) if selected_resume_id else "",
            }
                    
        except Exception as e:
            logger.error(f"❌ Error in _get_resume_detail for candidate {candidate_id}: {e}")
        
        return {"resume_text": "", "resume_id": ""}

    async def _get_resume_detail(self, candidate_id: str, client: httpx.AsyncClient, headers: dict) -> str:
        """Get full resume text using CandidateResumesDetail → ResumesTextDetail endpoint flow."""
        result = await self._get_resume_detail_with_id(candidate_id, client, headers)
        return result.get("resume_text", "")

    async def _get_resume_text_by_id(self, resume_id: str, client: httpx.AsyncClient, headers: dict) -> str:
        """Get resume text using a specific resume ID with ResumesTextDetail endpoint."""
        try:
            logger.debug(f"📖 Fetching resume text for resume ID: {resume_id}")
            
            resume_text_url = f"{self.api_url}/apiv2/bi/ResumesTextDetail"
            resume_response = await client.get(
                resume_text_url,
                params={"resumeIds": resume_id},
                headers=headers
            )
            
            if resume_response.status_code == 200:
                resume_detail = resume_response.json()
                
                # Handle different response structures
                if isinstance(resume_detail, dict):
                    resume_content = resume_detail.get("data", [{}])
                    if isinstance(resume_content, list) and resume_content:
                        resume_content = resume_content[0]
                    elif not isinstance(resume_content, dict):
                        resume_content = resume_detail
                else:
                    resume_content = resume_detail[0] if resume_detail else {}
                
                # Extract text from various possible fields
                resume_text = (resume_content.get("PLAINTEXT") or 
                             resume_content.get("plainText") or
                             resume_content.get("text") or 
                             resume_content.get("TEXT") or 
                             resume_content.get("resumeText") or "")
                
                if resume_text and resume_text.strip():
                    from html import unescape
                    logger.debug(f"Fetched resume text ({len(resume_text)} chars) for resume {resume_id}")
                    return unescape(resume_text.strip())
                else:
                    logger.debug(f"Resume text empty for resume ID {resume_id}")
            else:
                logger.debug(f"ResumesTextDetail failed for resume {resume_id}: {resume_response.status_code}")
                
        except Exception as e:
            logger.debug(f"Error fetching resume text for resume {resume_id}: {e}")
        
        return ""

    def _format_enhanced_candidate(self, applicant: Dict[str, Any], candidate_detail: Dict[str, Any], 
                                 resume_text: str, candidate_type: str) -> Dict[str, Any]:
        """Format enhanced candidate data for storage."""
        
        # Extract candidate ID and resume ID
        candidate_id = applicant.get("CANDIDATEID") or candidate_detail.get("CANDIDATEID") or ""
        resume_id = applicant.get("RESUMEID") or candidate_detail.get("RESUMEID") or ""
        
        # Extract basic info with fallbacks
        first_name = (get_field(applicant, ["FIRSTNAME", "firstName"]) or 
                 get_field(candidate_detail, ["FIRSTNAME", "firstName"]) or "")
        last_name = (get_field(applicant, ["LASTNAME", "lastName"]) or 
                get_field(candidate_detail, ["LASTNAME", "lastName"]) or "")
        full_name = f"{first_name} {last_name}".strip() or applicant.get("name", "") or candidate_detail.get("name", "") or "Professional Candidate"
        
        return {
            "jobdiva_id": applicant.get("JOBID") or candidate_detail.get("JOBID") or "",
            "candidate_id": candidate_id,
            "source": "JobDiva-Applicants" if candidate_type == "job_applicant" else "JobDiva-TalentSearch",
            "name": full_name,
            "firstName": first_name,
            "lastName": last_name,
            "email": get_field(candidate_detail, ["EMAIL", "email"]) or get_field(applicant, ["EMAIL", "email"]),
            "phone": _get_candidate_phone(candidate_detail) or _get_candidate_phone(applicant),
            "headline": (get_field(candidate_detail, ["TITLE", "title", "currentTitle"]) or 
                        get_field(applicant, ["TITLE", "title"]) or ""),
            "location": self._extract_location(candidate_detail) or self._extract_location(applicant),
            "work_city": get_field(applicant, ["workCity", "WORKCITY"]) or "",
            "work_state": get_field(applicant, ["workState", "WORKSTATE"]) or "",
            "work_location": ", ".join(p for p in [
                get_field(applicant, ["workCity", "WORKCITY"]) or "",
                get_field(applicant, ["workState", "WORKSTATE"]) or "",
            ] if p).strip(),
            "profile_url": get_field(candidate_detail, ["PROFILEURL", "profileUrl"]) or "",
            "image_url": get_field(candidate_detail, ["IMAGEURL", "imageUrl"]) or "",
            "resume_id": resume_id,
            "resume_text": resume_text,
            "data": {
                "applicant_data": applicant,
                "candidate_detail": candidate_detail,
                "skills": self._extract_skills(candidate_detail) or self._extract_skills(applicant),
                "experience": get_field(candidate_detail, ["EXPERIENCE", "experience"]) or "",
            },
            "status": "sourced"
        }

    async def update_candidate_resume_text(self, candidate_id: str) -> bool:
        """Update resume text for an existing candidate using new CandidateResumesDetail → ResumesTextDetail flow."""
        try:
            logger.info(f"🔄 Updating resume text for candidate: {candidate_id}")
            token = await self.authenticate()
            if not token:
                return False
                
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                resume_text = await self._get_resume_detail(candidate_id, client, headers)
                resume_id = None
                
                if resume_text and resume_text.strip():
                    from core.db import get_db_connection

                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE sourced_candidates
                                SET resume_text = %s, resume_id = %s, updated_at = CURRENT_TIMESTAMP
                                WHERE candidate_id = %s
                            """, (resume_text, resume_id, candidate_id))
                            
                            updated_rows = cur.rowcount
                            conn.commit()
                            
                            logger.info(f"✅ Updated resume text for {updated_rows} candidate records ({len(resume_text)} chars)")
                            return updated_rows > 0
                else:
                    logger.warning(f"⚠️ No resume text found for candidate {candidate_id}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error updating resume for candidate {candidate_id}: {e}")
            return False

    def _extract_location(self, data: Dict[str, Any]) -> str:
        """Extract formatted location from candidate data."""
        city = get_field(data, ["CITY", "city"]) or ""
        state = get_field(data, ["STATE", "state"]) or ""
        country = get_field(data, ["COUNTRY", "country"]) or ""
        
        location_parts = [city, state, country]
        return ", ".join([part for part in location_parts if part])

    def _extract_skills(self, data: Dict[str, Any]) -> List[str]:
        """Extract skills list from candidate data."""
        skills = get_field(data, ["SKILLS", "skills", "skillsList"]) or []
        if isinstance(skills, str):
            return [skill.strip() for skill in skills.split(",") if skill.strip()]
        elif isinstance(skills, list):
            return [str(skill) for skill in skills]
        return []

    async def save_enhanced_candidates_to_db(self, job_id: str, candidates: List[Dict[str, Any]]) -> int:
        """Save enhanced candidates to database with deduplication."""
        from services.sourced_candidates_storage import SourcedCandidatesStorage
        
        storage = SourcedCandidatesStorage()
        saved_count = 0
        
        for candidate in candidates:
            if storage.save_enhanced_candidate(job_id, candidate):
                saved_count += 1
        
        # Deduplicate after saving (prioritize job applicants over talent search)
        dedup_count = storage.deduplicate_candidates(job_id)
        logger.info(f"💾 Saved {saved_count} enhanced candidates, deduplicated {dedup_count}")
        
        return saved_count

    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Efficient job status check."""
        job = await self.get_job_by_id(job_id)
        if not job: return {"job_id": job_id, "status": "NOT_FOUND"}
        return {
            "job_id": job_id,
            "status": job.get("job_status", "OPEN"),
            "customer_name": job.get("customer_name", "Unknown"),
            "title": job.get("title", ""),
            "synced_at": readable_ist_now()
        }

    async def get_multiple_jobs_status(self, job_ids: List[str]) -> List[Dict[str, Any]]:
        """Batch fetch status for multiple jobs."""
        results = []
        for job_id in job_ids:
            results.append(await self.get_job_status(job_id))
        return results

    async def update_job_user_fields(self, job_id: str, fields: list) -> bool:
        """Update JobDiva UDFs with detailed logging."""
        token = await self.authenticate()
        if not token: 
            logger.error("❌ Sync failed: Could not authenticate with JobDiva")
            return False
        
        internal_id = job_id
        if "-" in str(job_id):
            logger.info(f"🔍 Resolving reference string {job_id} to JobDiva ID...")
            job_data = await self.get_job_by_id(job_id)
            if job_data: 
                internal_id = job_data.get("id")
                logger.info(f"✅ Resolved {job_id} to internal ID {internal_id}")
            else: 
                logger.error(f"❌ Failed to resolve {job_id} to a JobDiva internal ID")
                return False
            
        url = f"{self.api_url}/apiv2/jobdiva/updateJob"
        headers = {
            "Authorization": f"Bearer {token}", 
            "Content-Type": "application/json"
        }
        
        # Build a robust UDF list covering multiple JobDiva API variations
        normalized_fields = []
        for f in fields:
            val = str(f.get("userfieldValue") or f.get("value") or "")
            # Truncate to avoid JobDiva 4000-char limit
            if len(val) > 3950: val = val[:3950] + "..."
            
            normalized_fields.append({
                "userfieldId": str(f.get("userfieldId")), 
                "userfieldValue": val,
                "value": val # Some JobDiva v2 endpoints expect 'value'
            })
        
        # JobDiva API is notoriously inconsistent with casing between versions/endpoints
        # We provide redundant keys to ensure the payload is accepted
        payload = {
            "jobId": int(internal_id), 
            "jobid": int(internal_id),
            "userfields": normalized_fields,  # Standard lowercase
            "Userfields": normalized_fields   # Some v2 variations prefer Capital U
        }
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                logger.info(f"📡 Pushing UDF updates to JobDiva for Job {internal_id} (Ref: {job_id})...")
                logger.info(f"Payload Preview: {json.dumps(payload)[:200]}...")
                response = await client.post(url, json=payload, headers=headers)
                
                if response.status_code == 200:
                    logger.info(f"✅ JobDiva response: Success (200) for job {job_id}")
                    return True
                else:
                    logger.error(f"❌ JobDiva error ({response.status_code}): {response.text}")
                    return False
        except Exception as e: 
            logger.error(f"❌ HTTP Error during JobDiva UDF push: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def monitor_job_locally(self, job_id: str, data: dict) -> bool:
        """Enhanced monitor_job_locally with complete field coverage and validation"""
        if not self.engine:
            logger.error("Database engine not initialized for monitoring")
            return False
            
        debug_log(f"Starting monitor_job_locally for job {job_id}")
        
        try:
            with self.engine.connect() as conn:
                # Fail fast on row-lock and slow-statement contention.
                # monitor_job_locally is called from the 5-min poll loop, the
                # 15-min auto-sync, the /jobs/fetch handler, and several other
                # paths. The dashboard's `_get_monitored_jobs_sync` runs with a
                # 2s lock_timeout / 8s statement_timeout — without matching
                # caps on writers, a contested poll cycle can keep a pool slot
                # occupied for tens of seconds and starve concurrent reads.
                # The writes here are simple SELECT-then-UPDATE/INSERT on a
                # single monitored_jobs row; 500ms / 5s caps are well above
                # the steady-state cost but bounded enough that failures
                # surface in logs (return False, next cycle retries) instead
                # of compounding into dashboard 503s.
                conn.execute(text("SET LOCAL lock_timeout = '500ms'"))
                conn.execute(text("SET LOCAL statement_timeout = '5s'"))

                # Extract recruiter emails for job_configuration
                recruiter_emails = data.get("recruiter_emails", [])

                # Check if job exists in monitored_jobs by job_id OR jobdiva_id
                res = conn.execute(text("SELECT 1 FROM monitored_jobs WHERE job_id = :job_id OR jobdiva_id = :job_id"), {"job_id": job_id})
                exists = res.fetchone()
                
                if exists:
                    # Update monitored_jobs with ALL possible fields
                    import json
                    update_parts = []
                    params = {"job_id": job_id}
                    
                    # Define ALL possible columns that can be updated (including metrics fields)
                    valid_columns = [
                        # Core job identification
                        "job_id", "jobdiva_id", "title", "enhanced_title", "customer_name", "status",
                        
                        # Location information
                        "city", "state", "zip_code", "location_type",
                        
                        # Job details
                        "jobdiva_description", "ai_description", "recruiter_notes", 
                        "employment_type", "pay_rate", "openings", "work_authorization",
                        "posted_date", "start_date",

                        # Extended JobDiva fields
                        "priority", "program_duration", "max_allowed_submittals",
                        
                        # Application state
                        "processing_status", "processing_stage", "screening_level",
                        
                        # Lists and configurations
                        "selected_job_boards", "selected_employment_types", "recruiter_emails",
                        
                        # Metrics fields for UI display
                        "candidates_sourced", "resumes_shortlisted", "complete_submissions",
                        "pass_submissions", "pair_external_subs", "feedback_completed", "time_to_first_pass",
                        "pair_launched_at",

                        # Campaign grouping + phone-screen intro (inherited from a
                        # campaign when a job is added under one).
                        "campaign_id", "bot_introduction",
                        "sourcing_filters", "resume_match_filters",
                    ]
                    
                    # Fields where an empty string IS a valid intentional value (cleared UDFs or optional fields)
                    allow_empty_fields = {"recruiter_notes", "ai_description", "priority", "program_duration", "max_allowed_submittals"}
                    
                    for k, v in data.items():
                        if k in valid_columns:
                            # Skip None values always
                            if v is None:
                                continue
                            # For cleared-UDF fields, allow empty strings through
                            if v == "" and k not in allow_empty_fields:
                                continue
                                
                            # Special Protection: Never overwrite a real customer_name with "Unknown"
                            if k == "customer_name" and (str(v or "").lower() == "unknown" or not v):
                                # Skip this key to preserve the existing valid name in DB
                                continue
                            # Store empty string for fields that have no JobDiva value
                            if v == "" and k in {"priority", "program_duration", "max_allowed_submittals"}:
                                v = ""
                            # Clean location fields before storing
                            if k in ["city", "state", "zip"]:
                                v = _clean_location_field(v)
                                if not v:  # Skip empty location values
                                    continue

                                    
                            update_parts.append(f"{k} = :{k}")
                            if k in ["selected_employment_types", "selected_job_boards", "recruiter_emails", "enhancement_metadata", "sourcing_filters", "resume_match_filters"]:
                                if isinstance(v, (list, dict)):
                                    params[k] = json.dumps(v)
                                else:
                                    params[k] = v
                            else:
                                params[k] = v
                    
                    # Always update the timestamp
                    update_parts.append("updated_at = :updated_at")  
                    params["updated_at"] = readable_ist_now()
                    
                    if update_parts:
                        query = f"UPDATE monitored_jobs SET {', '.join(update_parts)} WHERE job_id = :job_id OR jobdiva_id = :job_id"
                        debug_log(f"Updating job {job_id} with fields: {list(params.keys())}")
                        conn.execute(text(query), params)
                    
                else:
                    # Insert into monitored_jobs with comprehensive field mapping
                    import json
                    params = {
                        "job_id": job_id,
                        
                        # Core job information
                        "status": data.get("status") or "OPEN",
                        "customer_name": data.get("customer_name") or "Unknown",
                        "title": data.get("title") or "",
                        
                        # Location information
                        "city": _clean_location_field(data.get("city")) or "",
                        "state": _clean_location_field(data.get("state")) or "",
                        "zip_code": _clean_location_field(data.get("zip_code") or data.get("zip")) or "",
                        "location_type": data.get("location_type") or "Onsite",
                        
                        # Job descriptions and content
                        "jobdiva_description": data.get("jobdiva_description") or "",
                        "ai_description": data.get("ai_description") or "",
                        "enhanced_title": data.get("enhanced_title") or data.get("title") or "",
                        "recruiter_notes": data.get("recruiter_notes") if data.get("recruiter_notes") is not None else (data.get("job_notes") or ""),
                        
                        # Employment details
                        "employment_type": data.get("employment_type") or "",
                        "work_authorization": data.get("work_authorization") or "",
                        "pay_rate": data.get("pay_rate") or "",
                        "openings": data.get("openings") or "",
                        
                        # Dates
                        "posted_date": data.get("posted_date") or "",
                        "start_date": data.get("start_date") or "",
                        
                        # Extended JobDiva fields — store empty if not provided by JobDiva
                        "priority": data.get("priority") or "",
                        "program_duration": data.get("program_duration") or "",
                        "max_allowed_submittals": data.get("max_allowed_submittals") or "",
                        
                        # Configuration and processing
                        "recruiter_emails": json.dumps(recruiter_emails) if recruiter_emails else '[]',
                        "selected_employment_types": json.dumps(data.get("selected_employment_types", [])),
                        "selected_job_boards": json.dumps(data.get("selected_job_boards", [])),
                        "screening_level": data.get("screening_level", "L1.5"),
                        "processing_status": data.get("processing_status", "pending"),

                        # Phone-screen intro + campaign grouping (both plain TEXT
                        # columns; campaign_id is NULL for standalone jobs).
                        "bot_introduction": data.get("bot_introduction") or "",
                        "campaign_id": data.get("campaign_id"),

                        # Identification
                        "job_id": job_id,
                        "jobdiva_id": data.get("jobdiva_id") or "",
                        
                        # Timestamps
                        "created_at": data.get("created_at") or readable_ist_now(),
                        "updated_at": readable_ist_now()
                    }
                    if data.get("sourcing_filters") is not None:
                        params["sourcing_filters"] = json.dumps(data.get("sourcing_filters")) if isinstance(data.get("sourcing_filters"), (list, dict)) else data.get("sourcing_filters")
                    if data.get("resume_match_filters") is not None:
                        params["resume_match_filters"] = json.dumps(data.get("resume_match_filters")) if isinstance(data.get("resume_match_filters"), (list, dict)) else data.get("resume_match_filters")
                    
                    # Build INSERT query dynamically based on available fields
                    columns = list(params.keys())
                    placeholders = [f":{col}" for col in columns]
                    
                    query = f"""
                        INSERT INTO monitored_jobs ({', '.join(columns)})
                        VALUES ({', '.join(placeholders)})
                    """
                    
                    debug_log(f"Inserting new job {job_id} with {len(columns)} fields")
                    conn.execute(text(query), params)

                conn.commit()
                debug_log(f"Successfully saved job {job_id} to monitored_jobs")
                return True
                
        except Exception as e:
            logger.error(f"Error monitoring job locally in DB: {e}")
            debug_log(f"Error monitoring job {job_id}: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return False

    def get_locally_monitored_job(self, job_id: str) -> dict:
        if not self.engine:
             return {}
        try:
            with self.engine.connect() as conn:
                # Get job data from monitored_jobs - Search BOTH Numeric ID and Hyphenated ID
                res = conn.execute(
                    text("SELECT * FROM monitored_jobs WHERE job_id = :job_id OR jobdiva_id = :job_id"), 
                    {"job_id": job_id}
                )
                row = res.fetchone()
                if row:
                    job_data = dict(row._mapping)
                    
                    # Parse JSON fields if they exist
                    import json
                    for field in ["recruiter_emails", "selected_employment_types", "selected_job_boards"]:
                        if job_data.get(field):
                            try:
                                if isinstance(job_data[field], str):
                                    job_data[field] = json.loads(job_data[field])
                            except (json.JSONDecodeError, TypeError):
                                job_data[field] = []
                        else:
                            job_data[field] = []
                    
                    return job_data
        except Exception as e:
            logger.error(f"Error fetching locally monitored job from DB: {e}")
        return {}
        
    def get_all_monitored_jobs(self) -> dict:
        if not self.engine:
            return {"jobs": {}}
        try:
            with self.engine.connect() as conn:
                res = conn.execute(text("SELECT * FROM monitored_jobs"))
                rows = res.fetchall()
                jobs = {}
                for row in rows:
                    j_dict = dict(row._mapping)
                    jid = j_dict.pop("job_id")
                    jobs[jid] = j_dict
                return {"jobs": jobs}
        except Exception as e:
            logger.error(f"Error fetching all monitored jobs from DB: {e}")
            return {"jobs": {}}

    def update_job_basic_info(self, job_id: str, update_data: dict) -> bool:
        """Update basic job information like employment_type, recruiter_notes, work_authorization, and recruiter_emails."""
        if not self.engine:
            logger.error("Database engine not initialized for updating job basic info")
            return False
            
        try:
            with self.engine.connect() as conn:
                # Build update query dynamically based on provided fields
                update_parts = []
                params = {"job_id": job_id}
                
                # Valid fields that can be updated
                valid_fields = ["employment_type", "recruiter_notes", "work_authorization", "recruiter_emails"]
                
                for field, value in update_data.items():
                    if field in valid_fields and value is not None:
                        if field == "recruiter_emails":
                            # Handle JSONB array for recruiter_emails
                            import json
                            update_parts.append(f"{field} = :{field}")
                            params[field] = json.dumps(value if isinstance(value, list) else [])
                        else:
                            update_parts.append(f"{field} = :{field}")
                            params[field] = value
                
                # Auto-extract work authorization if not explicitly provided but other fields are being updated
                if "work_authorization" not in update_data or not update_data["work_authorization"]:
                    work_auth = self._auto_extract_work_authorization(conn, job_id)
                    if work_auth:
                        update_parts.append("work_authorization = :work_authorization")
                        params["work_authorization"] = work_auth
                        logger.info(f"Auto-extracted work authorization for job {job_id}: {work_auth}")
                
                if not update_parts:
                    logger.warning(f"No valid fields to update for job {job_id}")
                    return False
                
                # Add updated timestamp
                update_parts.append("updated_at = :updated_at")  
                params["updated_at"] = readable_ist_now()
                
                # Execute update query - use SQLAlchemy text with proper parameter binding
                query = f"UPDATE monitored_jobs SET {', '.join(update_parts)} WHERE job_id = :job_id"
                logger.info(f"Executing update query: {query}")
                logger.info(f"Parameters: {params}")
                
                result = conn.execute(text(query), params)
                conn.commit()
                
                # Check if any rows were updated
                if result.rowcount > 0:
                    logger.info(f"Updated basic info for job {job_id}: {update_data}")
                    return True
                else:
                    logger.warning(f"No job found with ID {job_id}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error updating basic info for job {job_id}: {e}")
            return False

    def _auto_extract_work_authorization(self, conn, job_id: str) -> str:
        """Auto-extract work authorization from AI JD, job notes, or JobDiva description."""
        try:
            # Get job content for analysis
            result = conn.execute(text("""
                SELECT ai_description, recruiter_notes, jobdiva_description, enhanced_title, title
                FROM monitored_jobs 
                WHERE job_id = :job_id
            """), {"job_id": job_id})
            
            row = result.fetchone()
            if not row:
                return ""
                
            # Combine all available text for analysis
            ai_description, recruiter_notes, jobdiva_desc, enhanced_title, title = row
            
            combined_text = []
            if ai_description:
                combined_text.append(ai_description)
            if recruiter_notes:
                combined_text.append(recruiter_notes) 
            if jobdiva_desc:
                combined_text.append(jobdiva_desc)
            if enhanced_title:
                combined_text.append(enhanced_title)
            if title:
                combined_text.append(title)
                
            full_text = " ".join(combined_text).lower()
            
            if not full_text.strip():
                return ""
            
            # Work authorization patterns (prioritized by specificity)
            work_auth_patterns = [
                # Specific visa types
                ("H1B Transfer", ["h1b transfer", "h-1b transfer"]),
                ("H1B", ["h1b", "h-1b", "h1-b"]),
                ("Green Card", ["green card", "greencard", "permanent resident", "pr holder"]),
                ("US Citizen", ["us citizen", "u.s. citizen", "american citizen", "citizenship required"]),
                ("TN Visa", ["tn visa", "tn-visa", "nafta"]),
                ("L1 Visa", ["l1 visa", "l-1 visa", "l1-visa"]),
                ("EAD", ["ead", "employment authorization", "work authorization document"]),
                ("OPT", ["opt", "optional practical training"]),
                ("CPT", ["cpt", "curricular practical training"]),
                ("F1 Visa", ["f1 visa", "f-1 visa"]),
                # General categories  
                ("Work Authorization Required", ["work authorization required", "must be authorized", "legal right to work"]),
                ("No Sponsorship", ["no sponsorship", "cannot sponsor", "will not sponsor", "unable to sponsor"]),
                ("Sponsorship Available", ["sponsorship available", "will sponsor", "can sponsor", "visa sponsorship"]),
                ("Any Work Authorization", ["any work authorization", "all work authorization"])
            ]
            
            # Check patterns in order of specificity
            for auth_type, patterns in work_auth_patterns:
                for pattern in patterns:
                    if pattern in full_text:
                        logger.info(f"Auto-extracted work authorization '{auth_type}' from pattern '{pattern}' for job {job_id}")
                        return auth_type
                        
            # Fallback: check for generic work authorization terms
            generic_terms = ["visa", "authorization", "citizen", "resident", "sponsorship"]
            if any(term in full_text for term in generic_terms):
                return "Work Authorization Required"
                
            return ""
            
        except Exception as e:
            logger.error(f"Error auto-extracting work authorization for job {job_id}: {e}")
            return ""
    
    async def search_job_candidates_enhanced(
        self, 
        job_id: str,
        title_criteria: List = None,
        skill_criteria: List = None, 
        location_criteria: List = None,
        legacy_skills: List = None
    ) -> List[Dict[str, Any]]:
        """
        Enhanced job applicant search with separate title, skill, and location criteria.
        Applies intelligent filtering to job applicants based on multiple criteria types.
        """
        logger.info(f"🎯 Enhanced job applicant search for job {job_id}")
        
        try:
            # Build search criteria from enhanced format
            search_skills = []
            search_location = ""
            
            # Convert title criteria to searchable skills format
            if title_criteria:
                for title in title_criteria:
                    search_skills.append({
                        "value": title.value,
                        "priority": "Must Have" if title.match_type == "must" else "Flexible", 
                        "years_experience": title.years
                    })
                    
            # Convert skill criteria to searchable format
            if skill_criteria:
                for skill in skill_criteria:
                    search_skills.append({
                        "value": skill.value,
                        "priority": "Must Have" if skill.match_type == "must" else "Flexible",
                        "years_experience": skill.years
                    })
            
            # Use location criteria for location filtering
            if location_criteria:
                search_location = location_criteria[0].value
                
            # Fallback to legacy format if enhanced criteria not provided
            if not search_skills and legacy_skills:
                search_skills = legacy_skills
                
            logger.info(f"📋 Search criteria - Skills: {len(search_skills)}, Location: '{search_location}'")
            
            # Use existing search_candidates method with job_id to get applicants
            return await self.search_candidates(
                skills=search_skills,
                location=search_location,
                job_id=job_id  # This triggers job applicant search with filtering
            )
            
        except Exception as e:
            logger.error(f"Enhanced job applicant search failed for {job_id}: {e}")
            return []
    
    async def search_talent_pool_enhanced(
        self,
        title_criteria: List = None,
        skill_criteria: List = None,
        location_criteria: List = None, 
        legacy_skills: List = None,
        page: int = 1,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Enhanced talent pool search with separate title, skill, and location criteria.
        Searches broader talent database with intelligent multi-criteria filtering.
        """
        logger.info(f"🌐 Enhanced talent pool search - Page {page}, Limit {limit}")
        
        try:
            # Build search criteria from enhanced format
            search_skills = []
            search_location = ""
            
            # Convert title criteria to searchable skills format
            if title_criteria:
                for title in title_criteria:
                    search_skills.append({
                        "value": title.value,
                        "priority": "Must Have" if title.match_type == "must" else "Flexible",
                        "years_experience": title.years
                    })
                    
            # Convert skill criteria to searchable format  
            if skill_criteria:
                for skill in skill_criteria:
                    search_skills.append({
                        "value": skill.value,
                        "priority": "Must Have" if skill.match_type == "must" else "Flexible",
                        "years_experience": skill.years
                    })
            
            # Use location criteria for location filtering
            if location_criteria:
                search_location = location_criteria[0].value
                
            # Fallback to legacy format if enhanced criteria not provided
            if not search_skills and legacy_skills:
                search_skills = legacy_skills
                
            logger.info(f"📋 Talent search criteria - Skills: {len(search_skills)}, Location: '{search_location}'")
            
            # Use existing search_candidates method without job_id for talent pool
            return await self.search_candidates(
                skills=search_skills,
                location=search_location,
                page=page,
                limit=limit,
                job_id=None  # None triggers talent pool search
            )
            
        except Exception as e:
            logger.error(f"Enhanced talent pool search failed: {e}")
            return []

    async def talent_search_api(self, search_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Call JobDiva TalentSearch API with hierarchical search payload
        """
        token = await self.authenticate()
        if not token:
            logger.error("❌ TalentSearch failed: Could not authenticate with JobDiva")
            return []
        
        url = f"{self.api_url}/apiv2/jobdiva/TalentSearch"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        try:
            logger.info(f"🌐 Calling JobDiva TalentSearch API with {len(search_payload.get('advancedSkills', []))} skills, {len(search_payload.get('titles', []))} titles")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=search_payload, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    raw_candidates = data if isinstance(data, list) else (data.get("candidates") or data.get("results") or [])
                    
                    # Convert JobDiva response to standardized format
                    candidates = []
                    for candidate_data in raw_candidates:
                        try:
                            candidate = self._standardize_talent_candidate(candidate_data)
                            if candidate:
                                candidates.append(candidate)
                        except Exception as e:
                            logger.error(f"❌ Error processing talent candidate: {e}")
                            continue
                    
                    logger.info(f"✅ TalentSearch API returned {len(candidates)} candidates")
                    return candidates
                    
                else:
                    logger.error(f"❌ TalentSearch API error: {response.status_code} - {response.text}")
                    return []
                    
        except Exception as e:
            logger.error(f"❌ TalentSearch API call failed: {e}")
            return []

    async def create_candidate_note(
        self, 
        candidate_id: str, 
        job_id: str, 
        action: str, 
        note_text: str = "Click Here to view the report.",
        recruiter_id: int = 0
    ) -> Dict[str, Any]:
        """
        Create a candidate note in JobDiva (apiv2/jobdiva/createCandidateNote).
        Mapping for candidate feedback as per USER requirements.
        """
        token = await self.authenticate()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        url = f"{self.api_url}/apiv2/jobdiva/createCandidateNote"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # Handle job_id resolution (numeric JD ID required)
        jdiva_job_id = await self._resolve_jobdiva_job_id(job_id)
        if not jdiva_job_id:
            logger.warning(f"Could not resolve JobDiva Job ID for {job_id}")
            # Try to use it directly if it's numeric. Strip any -vN suffix first
            # so "26-06182-v2" doesn't digit-mash into a bogus 26061822.
            try:
                jdiva_job_id = int("".join(filter(str.isdigit, str(strip_job_version_suffix(job_id))))) if job_id else 0
            except (TypeError, ValueError):
                pass

        # JobDiva v2 action date format: yyyy-MM-dd'T'HH:mm:ss
        now = datetime.now(timezone.utc)
        action_date = now.strftime("%Y-%m-%dT%H:%M:%S")

        payload = {
            "candidateid": int(candidate_id),
            "note": note_text,
            "recruiterid": recruiter_id,  # PAIR recruiter ID — set via JOBDIVA_PAIR_RECRUITER_ID env var
            "action": action,
            "actionDate": action_date,
            "link2AnOpenJob": jdiva_job_id if jdiva_job_id else 0,
            "setAsAuto": True
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                debug_log(f"Creating JobDiva Note for Candidate {candidate_id}: {action}")
                response = await client.post(url, headers=headers, json=payload)
                
                if response.status_code in [200, 201]:
                    logger.info(f"✅ Created JobDiva note for candidate {candidate_id}")
                    return {"status": "success", "data": response.json() if response.text else {}}
                else:
                    logger.error(f"❌ Failed to create JobDiva note: {response.status_code} - {response.text}")
                    return {"status": "error", "message": response.text, "code": response.status_code}
        except Exception as e:
            logger.error(f"❌ Exception creating JobDiva note: {e}")
            return {"status": "error", "message": str(e)}

    async def create_candidate_sticky_note(
        self, 
        candidate_id: str, 
        note_text: str,
        recruiter_id: int = 0
    ) -> Dict[str, Any]:
        """
        Create a sticky note in JobDiva (apiv2/jobdiva/createCandidateStickyNote).
        Sticky notes are pinned to the top of the candidate's notes section.
        """
        token = await self.authenticate()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        url = f"{self.api_url}/apiv2/jobdiva/createCandidateStickyNote"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "candidateid": int(candidate_id),
            "note": note_text,
            "recruiterid": recruiter_id
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"📤 Creating JobDiva Sticky Note for Candidate {candidate_id}")
                response = await client.post(url, headers=headers, json=payload)
                
                if response.status_code in [200, 201]:
                    logger.info(f"✅ Created JobDiva sticky note for candidate {candidate_id}")
                    return {"status": "success", "data": response.json() if response.text else {}}
                else:
                    logger.warning(f"⚠️ Failed to create sticky note (Status {response.status_code}). JobDiva might not support this endpoint. Falling back to standard note.")
                    return {"status": "error", "message": response.text, "code": response.status_code}
        except Exception as e:
            logger.error(f"❌ Exception creating JobDiva sticky note: {e}")
            return {"status": "error", "message": str(e)}

    async def update_candidate_qualification(
        self,
        candidate_id: str,
        qualification_name: str = "PAIR Candidates",
        value: str = "PASS",
        recruiter_id: int = 0,
        update_date: Optional[str] = None,
        qualification_type_id: int = 0
    ) -> Dict[str, Any]:
        """
        Update a candidate qualification in JobDiva (apiv2/jobdiva/updateCandidateQualifications).
        """
        token = await self.authenticate()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        url = f"{self.api_url}/apiv2/jobdiva/updateCandidateQualifications"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # Date format: yyyy-MM-dd'T'HH:mm:ss
        if not update_date:
            update_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        elif "T" not in update_date:
            try:
                dt = datetime.fromisoformat(update_date.replace("Z", "+00:00"))
                update_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
            except:
                update_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        # Based on JobDiva v2 swagger and user requirements:
        # qualificationValue = "PASS"
        # qualificationTypeId is the numeric ID for "PAIR Candidates"
        # If we only have the name, we try to pass it without the ID if ID is 0
        qual_obj = {
            "qualification": qualification_name,
            "qualificationValue": value,
            "date": update_date,
            "recruiterid": recruiter_id
        }
        if qualification_type_id and qualification_type_id > 0:
            qual_obj["qualificationTypeId"] = qualification_type_id

        payload = {
            "candidateid": int(candidate_id),
            "overwrite": False,  # Prevent clearing existing qualifications
            "qualifications": [qual_obj]
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"📤 Updating JobDiva Qualification for Candidate {candidate_id}: {qualification_name}={value}")
                response = await client.post(url, headers=headers, json=payload)
                
                if response.status_code in [200, 201]:
                    logger.info(f"✅ Updated JobDiva qualification for candidate {candidate_id}")
                    return {"status": "success", "data": response.json() if response.text else {}}
                else:
                    logger.error(f"❌ Failed to update JobDiva qualification: {response.status_code} - {response.text}")
                    return {"status": "error", "message": response.text, "code": response.status_code}
        except Exception as e:
            logger.error(f"❌ Exception updating JobDiva qualification: {e}")
            return {"status": "error", "message": str(e)}
    
    async def pin_candidate_note(
        self,
        note_id: int,
        is_pinned: bool = True
    ) -> Dict[str, Any]:
        """
        Pin or Unpin a candidate note (GET /apiv2/jobdiva/pinUnPinCandidateNotes).
        """
        token = await self.authenticate()
        if not token:
            return {"status": "error", "message": "Authentication failed"}

        url = f"{self.api_url}/apiv2/jobdiva/pinUnPinCandidateNotes"
        headers = {
            "Authorization": f"Bearer {token}"
        }
        params = {
            "candidateNoteIds": [note_id],
            "isPinned": is_pinned
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"📌 {'Pinning' if is_pinned else 'Unpinning'} JobDiva Note {note_id}")
                # Swagger shows it as a GET request with array params
                response = await client.get(url, headers=headers, params=params)
                
                if response.status_code in [200, 201]:
                    logger.info(f"✅ Successfully {'pinned' if is_pinned else 'unpinned'} note {note_id}")
                    return {"status": "success", "data": response.json() if response.text else {}}
                else:
                    logger.error(f"❌ Failed to pin/unpin note: {response.status_code} - {response.text}")
                    return {"status": "error", "message": response.text, "code": response.status_code}
        except Exception as e:
            logger.error(f"❌ Exception pinning JobDiva note: {e}")
            return {"status": "error", "message": str(e)}

    def _standardize_talent_candidate(self, candidate_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Convert JobDiva TalentSearch candidate to standardized format
        """
        try:
            # Extract basic info
            candidate_id = str(get_field(candidate_data, ["candidateId", "id", "ID"]) or "")
            if not candidate_id:
                return None
            
            first_name = get_field(candidate_data, ["firstName", "firstname", "FIRSTNAME"]) or ""
            last_name = get_field(candidate_data, ["lastName", "lastname", "LASTNAME"]) or ""
            if not is_valid_candidate_name(first_name, last_name):
                logger.warning("Standardize rejecting invalid name: first=%r last=%r id=%r", first_name, last_name, candidate_id)
                return None
            name = f"{first_name} {last_name}".strip() or "Unknown Candidate"
            
            # Extract location
            city = get_field(candidate_data, ["city", "locationCity", "CITY"]) or ""
            state = get_field(candidate_data, ["state", "locationState", "STATE"]) or ""
            location = f"{city}, {state}".strip(", ") if city or state else ""
            
            # Extract skills
            skills_raw = get_field(candidate_data, ["skills", "SKILLS", "skillsList"]) or []
            skills = []
            if isinstance(skills_raw, str):
                skills = [skill.strip() for skill in skills_raw.split(",") if skill.strip()]
            elif isinstance(skills_raw, list):
                skills = [str(skill) for skill in skills_raw if skill]
            
            # Extract experience
            years_exp = 0
            exp_raw = get_field(candidate_data, ["experience", "yearsExperience", "totalExperience"]) or "0"
            try:
                years_exp = int(float(str(exp_raw)))
            except (ValueError, TypeError):
                years_exp = 0
            
            # Extract resume data
            resume_text = self._extract_resume_text(candidate_data) or ""
            resume_url = get_field(candidate_data, ["resumeUrl", "resume_url"]) or ""
            
            # Extract companies from resume text
            companies = self._extract_companies_from_resume(resume_text)
            
            return {
                "candidateId": candidate_id,
                "name": name,
                "firstName": first_name,
                "lastName": last_name,
                "email": get_field(candidate_data, ["email", "EMAIL"]) or "",
                "phone": _get_candidate_phone(candidate_data),
                "title": get_field(candidate_data, ["title", "currentTitle", "TITLE"]) or "",
                "location": location,
                "city": city,
                "state": state,
                "skills": skills,
                "experience": years_exp,
                "companies": companies,
                "resumeText": resume_text,
                "resumeUrl": resume_url,
                "source": "talent_search"
            }
            
        except Exception as e:
            logger.error(f"❌ Error standardizing talent candidate: {e}")
            return None
    
    def _extract_companies_from_resume(self, resume_text: str) -> List[str]:
        """
        Extract company names from resume text using simple pattern matching
        """
        if not resume_text:
            return []
        
        companies = []
        
        # Common patterns for company identification in resumes
        import re
        
        # Look for patterns like "Company Name, City" or "Company Name - Title"
        company_patterns = [
            r'(?:^|\n)([A-Z][A-Za-z\s&\.,-]+?)\s*(?:,\s*[A-Z]{2}|,\s*\w+\s*[A-Z]{2}|\s*-\s*)',
            r'(?:at|@)\s+([A-Z][A-Za-z\s&\.,-]+?)(?:\s*,|\s*\n|$)',
            r'(?:Company|Employer|Organization):\s*([A-Za-z\s&\.,-]+)',
        ]
        
        for pattern in company_patterns:
            matches = re.findall(pattern, resume_text, re.MULTILINE)
            for match in matches:
                company = match.strip()
                # Filter out common non-company words
                if (len(company) > 2 and 
                    company not in ['Inc', 'LLC', 'Corp', 'Ltd', 'Company'] and
                    not any(word in company.lower() for word in ['experience', 'education', 'skills', 'summary'])):
                    companies.append(company)
        
        # Remove duplicates and limit to reasonable number
        unique_companies = list(dict.fromkeys(companies))[:10]
        return unique_companies

    async def search_candidate_profile(self, email: str, first_name: str = None, last_name: str = None) -> Optional[int]:
        """
        Search for an existing candidate using POST (more fields).
        """
        token = await self.authenticate()
        if not token:
            return None

        url = f"{self.api_url}/apiv2/jobdiva/searchCandidateProfile"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        payload = {"email": email}
        if first_name: payload["firstName"] = first_name
        if last_name: payload["lastName"] = last_name

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        # Return the first match's ID
                        return data[0].get("candidateId") or data[0].get("CANDIDATEID")
                else:
                    # Fallback to GET if POST is not available or fails with 405
                    if response.status_code == 405:
                        res_get = await client.get(url, params={"email": email}, headers=headers)
                        if res_get.status_code == 200:
                            data_get = res_get.json()
                            if data_get and isinstance(data_get, list) and len(data_get) > 0:
                                return data_get[0].get("candidateId")
        except Exception as e:
            logger.error(f"❌ searchCandidateProfile failed: {e}")
        return None

    async def create_candidate(self, first_name: str, last_name: str, email: str, phone: str = "") -> Optional[int]:
        """
        Create a new candidate in JobDiva.
        """
        token = await self.authenticate()
        if not token:
            return None

        url = f"{self.api_url}/apiv2/jobdiva/createCandidate"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "firstName": first_name,
            "lastName": last_name,
            "email": email,
            "phone": phone,
            "candidateSource": "PAIR-Sourced"
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                logger.info(f"🔎 createCandidate response: {response.status_code} — {response.text[:300]}")
                if response.status_code in [200, 201]:
                    data = response.json()
                    if isinstance(data, dict):
                        cid = data.get("candidateId") or data.get("id") or data.get("CANDIDATEID")
                        logger.info(f"✅ createCandidate: created candidateId={cid}, response keys={list(data.keys())}")
                        return cid
                    logger.info(f"✅ createCandidate: returned raw ID={data}")
                    return data  # If it's directly the ID
                else:
                    logger.error(f"❌ createCandidate failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"❌ createCandidate exception: {e}")
        return None

    async def create_job_application_with_resume(
        self,
        candidate_id: Any,
        job_id: Any,
        resume_text: str = "",
        filename: str = "candidate_resume.txt",
        first_name: str = "",
        last_name: str = "",
        email: str = "",
        phone: str = ""
    ) -> tuple:
        """
        Creates a job application via JSON (application/json).
        After creation, updates the candidate's name, email, and phone since
        the JSON endpoint creates 'Unknown Unknown' with an Auto_ placeholder
        email. We fix name + real contact info immediately via updateCandidateProfile.
        Returns (success: bool, new_candidateId: int|None).
        """
        token = await self.authenticate()
        if not token:
            return False, None

        # Check if candidate already exists to avoid duplicate/Unknown-Unknown profile
        if email and not candidate_id:
            candidate_id = await self.search_candidate_profile(email, first_name, last_name)

        from datetime import datetime
        resume_date = datetime.now().strftime("%m/%d/%Y 12:00:00")

        url = f"{self.api_url}/apiv2/jobdiva/CreateJobApplicationWithResume"
        # Resolve to the real numeric JobDiva job id. This correctly handles a
        # numeric id, a reference string (26-06182) AND a versioned ref
        # (26-06182-v2 -> root job), instead of digit-mashing the ref into a
        # bogus number. Fall back to digit extraction only if resolution fails.
        resolved_job_id = await self._resolve_jobdiva_job_id(job_id)
        if not resolved_job_id:
            try:
                resolved_job_id = int("".join(filter(str.isdigit, str(strip_job_version_suffix(job_id))))) if job_id else 0
            except (TypeError, ValueError):
                resolved_job_id = 0

        # Build an explicit text header to guarantee JobDiva's parser correctly 
        # extracts the confirmed candidate name and contact info.
        header_lines = []
        if first_name or last_name:
            header_lines.append(f"Name: {first_name} {last_name}".strip())
        if email:
            header_lines.append(f"Email: {email}")
        if phone:
            header_lines.append(f"Phone: {phone}")
        
        if header_lines:
            header_text = "\n".join(header_lines)
            resume_text = f"{header_text}\n\n================================\n\n{resume_text}"

        json_payload = {
            "filename": filename,
            "textfile": resume_text,
            "filecontent": "",
            "jobid": int(resolved_job_id or 0),
            "recruiterid": int(JOBDIVA_PAIR_RECRUITER_ID or 0),
            "resumeDate": resume_date,
            "resumesource": 0
        }
        if candidate_id:
            json_payload["candidateid"] = int(candidate_id)

        try:
            for attempt in range(2):
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        url,
                        json=json_payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                        }
                    )
                status, res_body = response.status_code, response.text
                
                if status == 401 and attempt == 0:
                    logger.warning(f"⚠️ CreateJobApplicationWithResume got 401. Refreshing token...")
                    token = await self.authenticate(force_refresh=True)
                    if not token:
                        return False, None
                    continue

                logger.info(f"🔎 CreateJobApplicationWithResume: {status} — {res_body[:200]}")
                break

            if status in [200, 201]:
                try:
                    new_cid = int(res_body.strip())
                except (ValueError, TypeError):
                    # JobDiva may return JSON instead of a bare integer
                    try:
                        import json as _json
                        parsed = _json.loads(res_body)
                        if isinstance(parsed, dict):
                            new_cid = parsed.get("candidateId") or parsed.get("id") or parsed.get("CANDIDATEID")
                        else:
                            new_cid = None
                    except Exception:
                        new_cid = None

                # When linking an existing candidate, JobDiva often returns 0 or empty
                # body (no new profile created). Fall back to the pre-found candidate_id
                # so the ID is correctly persisted and updateCandidateProfile still runs.
                if not new_cid and candidate_id:
                    new_cid = candidate_id
                    logger.info(f"ℹ️ JobDiva returned no ID — using pre-found candidateId={new_cid}")

                logger.info(f"✅ JobDiva application linked/created → candidateId={new_cid}, job={job_id}")

                # We injected the name into the resume header, so JobDiva's parser should 
                # extract it perfectly. We still call _update_candidate_name instantly 
                # just to guarantee the exact spelling and apply any missing fields.
                if new_cid and (first_name or last_name or email or phone):
                    await self._update_candidate_name(token, new_cid, first_name, last_name, email, phone)

                return True, new_cid
            else:
                logger.error(f"❌ CreateJobApplicationWithResume failed: {status} - {res_body}")
        except Exception as e:
            logger.error(f"❌ CreateJobApplicationWithResume exception: {e}")
        return False, None

    async def _update_candidate_name(self, token: str, candidate_id: int, first_name: str, last_name: str, email: str = "", phone: str = "") -> bool:
        """
        Updates a JobDiva candidate's name, email, and phone after creation.
        Used to fix 'Unknown Unknown' + Auto_ placeholder email created by
        CreateJobApplicationWithResume JSON mode.
        Endpoint: POST /apiv2/jobdiva/updateCandidateProfile

        """
        url = f"{self.api_url}/apiv2/jobdiva/updateCandidateProfile"
        payload = {
            "candidateid": candidate_id,
            "firstName": first_name,
            "lastName": last_name,
        }
        # Set real email so JobDiva doesn't keep the Auto_ placeholder
        if email and not email.lower().startswith("auto_"):
            payload["email"] = email
        if phone:
            payload["phone"] = phone
            
        try:
            for attempt in range(2):
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json"
                        }
                    )
                
                if response.status_code == 401 and attempt == 0:
                    logger.warning(f"⚠️ updateCandidateProfile got 401. Refreshing token...")
                    token = await self.authenticate(force_refresh=True)
                    if not token:
                        return False
                    continue

                logger.info(f"🔎 updateCandidateProfile response: {response.status_code} — {response.text[:300]}")
                break
            if response.status_code in [200, 201]:
                logger.info(f"✅ Profile updated for candidateId={candidate_id}: {first_name} {last_name}, email={bool(email)}, phone={bool(phone)}")
                return True
            else:
                logger.warning(f"⚠️ updateCandidateProfile failed: {response.status_code} - {response.text[:300]}")
        except Exception as e:
            logger.warning(f"⚠️ updateCandidateProfile exception: {e}")
        return False




    async def get_job_applicants_detail(self, job_id: int) -> List[Dict[str, Any]]:
        """
        Fetch the list of applicants for a job from JobDiva (apiv2/bi/JobsApplicantsDetail).
        """
        token = await self.authenticate()
        if not token:
            return []

        url = f"{self.api_url}/apiv2/bi/JobsApplicantsDetail"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        # Resolve to numeric ID if reference ID (hyphenated) is provided
        resolved_id = await self._resolve_jobdiva_job_id(str(job_id))
        safe_job_id = resolved_id if resolved_id else job_id
        
        try:
            params = {"jobIds": [int(safe_job_id)]}
        except (ValueError, TypeError):
            logger.error(f"❌ get_job_applicants_detail: Invalid job_id {safe_job_id}")
            return []

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    return data if isinstance(data, list) else (data.get("data") or [])
                else:
                    logger.error(f"❌ getJobApplicantsDetail failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"❌ getJobApplicantsDetail exception: {e}")
        return []

    async def is_candidate_applied_to_job(self, job_id: int, candidate_id: int) -> bool:
        """
        Check if a candidate is already applied to a job.
        """
        applicants = await self.get_job_applicants_detail(job_id)
        for app in applicants:
            cid = get_field(app, ["candidateId", "CANDIDATEID", "ID", "id"])
            if cid and int(cid) == int(candidate_id):
                return True
        return False

    async def get_job_submittals(self, job_id, none_on_error: bool = False) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch manual candidate submittals for a job from JobDiva BI endpoint.
        Uses /apiv2/bi/JobSubmittalsDetail. Returns list of submittal records.
        Each record includes CANDIDATEID, RECIPIENTNAME, SUBMITDATE fields.

        none_on_error=True makes fetch failures distinguishable from a
        genuinely empty submittal list (returns None instead of []) — the
        auto-sync persistence path must not wipe previously stored submittals
        on a transient JobDiva outage.
        """
        error_result = None if none_on_error else []
        token = await self.authenticate()
        if not token:
            return error_result

        # Resolve to numeric JobDiva ID
        resolved_id = await self._resolve_jobdiva_job_id(str(job_id))
        safe_job_id = resolved_id if resolved_id else job_id

        try:
            numeric_id = int(safe_job_id)
        except (ValueError, TypeError):
            logger.error(f"❌ get_job_submittals: Invalid job_id '{safe_job_id}'")
            return error_result

        url = f"{self.api_url}/apiv2/bi/JobSubmittalsDetail"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        params = {"jobIds": [numeric_id]}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    result = data if isinstance(data, list) else (data.get("data") or [])
                    logger.debug(f"📋 get_job_submittals: {len(result)} records for job {numeric_id}")
                    return result
                else:
                    logger.error(f"❌ get_job_submittals failed: {response.status_code} - {response.text[:300]}")
        except Exception as e:
            logger.error(f"❌ get_job_submittals exception: {e}")
        return error_result

    async def _fetch_candidate_qualifications_batch(
        self,
        token: str,
        candidate_ids: List[str],
        chunk_size: int = 50,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch CandidatesQualificationsDetail in batch and group by candidate ID."""
        ids = [str(cid).strip() for cid in (candidate_ids or []) if cid and str(cid).strip()]
        if not ids:
            return {}

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        endpoint = f"{self.api_url}/apiv2/bi/CandidatesQualificationsDetail"
        chunks = [ids[i:i + chunk_size] for i in range(0, len(ids), chunk_size)]
        results: Dict[str, List[Dict[str, Any]]] = {}

        for chunk in chunks:
            try:
                numeric_ids = [int(cid) for cid in chunk if cid.isdigit()]
                if not numeric_ids:
                    continue
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(
                        endpoint,
                        params={"candidateIds": numeric_ids},
                        headers=headers,
                    )
                if response.status_code == 200:
                    data = response.json()
                    payload = data if isinstance(data, list) else (data.get("data") or [])
                    if payload and len(payload) > 0:
                        sample = payload[0] if isinstance(payload[0], dict) else {}
                        logger.info(f"CandidatesQualificationsDetail sample keys: {list(sample.keys())[:10]}")
                    for q_row in payload:
                        if not isinstance(q_row, dict): continue
                        # Try every possible candidate ID key JobDiva might use
                        cid = str(
                            q_row.get("CANDIDATEID") or
                            q_row.get("candidateId") or
                            q_row.get("CONTACTID") or
                            q_row.get("contactId") or
                            q_row.get("ID") or
                            q_row.get("id") or ""
                        )
                        if cid:
                            if cid not in results:
                                results[cid] = []
                            results[cid].append(q_row)
                else:
                    logger.warning(f"CandidatesQualificationsDetail HTTP {response.status_code} for ids={chunk[:3]}")
            except Exception as e:
                logger.warning(f"CandidatesQualificationsDetail batch fetch failed: {e}")
        logger.info(f"CandidatesQualificationsDetail: fetched qualifications for {len(results)} candidates out of {len(ids)} requested")
        return results

    async def get_candidate_qualifications(self, candidate_id: str) -> List[Dict[str, Any]]:
        """
        Fetch qualification history for a candidate from JobDiva BI endpoint.
        Uses /apiv2/bi/CandidatesQualificationsDetail.
        Each record includes QUALIFICATION, QUALIFICATIONVALUE, DATECREATED fields.
        """
        token = await self.authenticate()
        if not token:
            return []

        try:
            numeric_cid = int(candidate_id)
        except (ValueError, TypeError):
            logger.error(f"❌ get_candidate_qualifications: Invalid candidate_id '{candidate_id}'")
            return []

        url = f"{self.api_url}/apiv2/bi/CandidatesQualificationsDetail"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        params = {"candidateIds": [numeric_cid]}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    result = data if isinstance(data, list) else (data.get("data") or [])
                    logger.debug(f"📋 get_candidate_qualifications: {len(result)} records for candidate {numeric_cid}")
                    return result
                else:
                    logger.error(f"❌ get_candidate_qualifications failed: {response.status_code} - {response.text[:300]}")
        except Exception as e:
            logger.error(f"❌ get_candidate_qualifications exception: {e}")
        return []


jobdiva_service = JobDivaService()
