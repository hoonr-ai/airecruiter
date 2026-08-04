import re


def is_remote_job(location_type: str, city: str = "") -> bool:
    """True when the role should be presented as fully remote.

    Canonical synonym list shared by all backend call sites. Matches the
    frontend isRemoteJob() heuristic in apps/web/app/jobs/new/page.tsx.
    """
    norm = (location_type or "").strip().lower().replace("-", "").replace("_", "")
    if "remote" in norm or "virtual" in norm or "telecommute" in norm or norm == "wfh":
        return True
    return (city or "").strip().upper() == "REMOTE"


def normalize_skill(skill_name: str) -> str:
    """
    Converts a skill name to a canonical slug.
    Rule: Lowercase, strip whitespace, replace non-alphanumeric (except + and #) with underscores.
    Preserves: 'c++', 'c#', 'node.js' -> 'node_js' (dots usually handled by regex depending on pref)
    
    Example: 
    "Apache Spark " -> "apache_spark"
    "C++" -> "c++"
    "Node.js" -> "node_js"
    """
    if not skill_name:
        return ""
    
    # Lowercase and strip
    s = skill_name.lower().strip()
    
    # Replace dots and spaces with underscores
    s = re.sub(r'[\s\.]+', '_', s)
    
    # Remove everything else that isnt alphanumeric, underscore, +, or #
    s = re.sub(r'[^a-z0-9_+#]', '', s)
    
    return s.strip('_')

def is_valid_phone(phone_str: str) -> bool:
    """
    Validates a phone number string to ensure it's not a dummy/masked number.
    Rejects strings containing sequences like 000000, 999999, etc.
    """
    if not phone_str:
        return False
        
    # Strip all non-digit characters to check the underlying number
    digits = re.sub(r'\D', '', str(phone_str))
    
    # Phone number must have at least 10 digits
    if len(digits) < 10:
        return False
        
    # Reject known dummy patterns often used by Job Boards (e.g. 94657-000000, 999-999-9999)
    if "000000" in digits or "999999" in digits or "111111" in digits:
        return False
        
    return True
