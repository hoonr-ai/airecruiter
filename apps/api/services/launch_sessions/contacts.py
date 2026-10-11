"""A session row's contact layers, and the effective row the rules see.

``launch_session_candidates.contact`` (jsonb) holds, per candidate:

  done / outcome / looked_up / kipplo / phone_provider / email_provider
                S3's lookup bookkeeping (``done`` = this row needs no lookup
                any more: resumable per row)
  override      {phone?, email?} a lookup found — REPLACES the row's own
                contact (browser: ``override.x || c.x``)
  provided      {phone?, email?} the recruiter typed in the needs-contact
                modal; a field left empty keeps the lookup's value
                (``mergeContactOverrides``)
  final         {phone, email} confirmed in the QA review: replaces BOTH
                channels, an empty field is null — never a fallback to the
                row's own or a looked-up contact (review-parity #12), so QA
                can never dial a real candidate it overrode
  needs         S5's needs-info entry (what is missing, duplicates)
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, Optional

from services.launch_sessions import rules

_CONTACT_KEYS = ("email", "workEmail", "personalEmail", "phone", "mobilePhone")
_ZOOMINFO_KEYS = ("workEmail", "personalEmail", "mobilePhone", "workPhone", "phone", "email")


def _clean(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    return s or None


def override_of(contact: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The {phone?, email?} override the browser rules apply for a row whose
    contacts are not final: the lookup's, with the recruiter's provided
    values on top (an empty provided field keeps the lookup's)."""
    contact = contact or {}
    if contact.get("final") is not None:
        return None
    pending = contact.get("override") if isinstance(contact.get("override"), dict) else None
    provided = contact.get("provided") if isinstance(contact.get("provided"), dict) else None
    if provided is None:
        return dict(pending) if pending else None
    merged = rules.merge_contact_overrides({"x": pending} if pending is not None else {}, {"x": provided})
    return merged.get("x") or None


def apply_final_contacts(row: Dict[str, Any], phone: Any, email: Any) -> Dict[str, Any]:
    """A copy of ``row`` whose ONLY contacts are ``phone`` / ``email``: every
    field the launch getters read (top level, enhanced_info, data,
    data.enhanced_info, data.zoominfo_contact_enrichment) is cleared first."""
    out = copy.deepcopy(row or {})
    for key in _CONTACT_KEYS:
        out.pop(key, None)
    for nested in ("enhanced_info", "data"):
        if isinstance(out.get(nested), dict):
            for key in _CONTACT_KEYS:
                out[nested].pop(key, None)
    data = out.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("enhanced_info"), dict):
            for key in _CONTACT_KEYS:
                data["enhanced_info"].pop(key, None)
        if isinstance(data.get("zoominfo_contact_enrichment"), dict):
            for key in _ZOOMINFO_KEYS:
                data["zoominfo_contact_enrichment"].pop(key, None)
    out["phone"] = _clean(phone) or ""
    out["email"] = _clean(email) or ""
    return out


def effective_row(cand: Dict[str, Any]) -> Dict[str, Any]:
    """The row the S5/S6 rules run on: final contacts applied when the row
    was confirmed in review, else the row itself (overrides apply separately)."""
    row = cand.get("row") or {}
    final = (cand.get("contact") or {}).get("final")
    if isinstance(final, dict):
        return apply_final_contacts(row, final.get("phone"), final.get("email"))
    return row


def overrides_for(cands: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{candidate_id: override} for every row with one (see override_of)."""
    out: Dict[str, Dict[str, Any]] = {}
    for c in cands:
        ov = override_of(c.get("contact"))
        if ov:
            out[str(c["candidate_id"])] = ov
    return out


def current_contact(cand: Dict[str, Any]) -> Dict[str, str]:
    """What the review / needs-contact modals show: override || the row's own."""
    row = effective_row(cand)
    ov = override_of(cand.get("contact")) or {}
    return {
        "phone": str(ov.get("phone") or rules.get_candidate_launch_phone(row) or ""),
        "email": str(ov.get("email") or rules.get_candidate_launch_email(row) or ""),
    }


def is_kipplo_issue(outcome: Any) -> bool:
    """lib/contact-lookup.ts isKipploIssue: Kipplo said something other than
    a hit (phone/email) or a plain miss."""
    value = str(outcome or "").strip()
    if not value or value in ("-", "miss"):
        return False
    return not all(f in ("email", "phone") for f in value.split(","))
