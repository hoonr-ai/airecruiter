"""Phone normalization for DNC matching.

Returns the digit-only North-American 11-digit form (e.g. "14408405137") or
None when the input cannot be normalized. Same rules are mirrored in the
frontend at apps/web/lib/phone.ts so client- and server-side checks agree.
"""

from typing import Optional
import re


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    if len(digits) == 10:
        return "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return digits
    return None
