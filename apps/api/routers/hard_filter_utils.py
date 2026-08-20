"""Shared hard-filter token classification for webhook write and UI read paths."""
from typing import Any, Dict, List, Optional

_HF_PASS = frozenset({"passed", "pass"})
_HF_FAIL = frozenset({"failed", "fail"})
_HF_PENDING = frozenset({"pending", "in_progress", "awaiting"})
_HF_EXCLUDE = frozenset({"not_hard_filter"})


def hard_filter_row_display(raw: Optional[str]) -> Optional[str]:
    """Pass/Fail/Pending for a hard-filter row, or None if not a hard filter."""
    token = str(raw or "").lower().strip()
    if not token or token in _HF_EXCLUDE:
        return None
    if token in _HF_PASS:
        return "Pass"
    if token in _HF_FAIL:
        return "Fail"
    if token in _HF_PENDING:
        return "Pending"
    return None


def row_hf_token(item: Dict[str, Any]) -> str:
    if hasattr(item, "model_dump"):
        item = item.model_dump(mode="json")
    return str(item.get("hard_filter_status") or item.get("pass_fail") or "").lower().strip()


def count_pending_hard_filters(
    hard_filter_results: Optional[List[Any]] = None,
    transcriptions: Optional[List[Any]] = None,
) -> int:
    """Count unresolved hard-filter rows, preferring hard_filter_results over transcriptions."""
    rows = hard_filter_results if hard_filter_results else transcriptions or []
    seen: set[str] = set()
    pending = 0
    for item in rows:
        data = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        if not isinstance(data, dict):
            continue
        if hard_filter_row_display(row_hf_token(data)) != "Pending":
            continue
        key = (data.get("question") or "").strip().lower() or str(id(item))
        if key in seen:
            continue
        seen.add(key)
        pending += 1
    return pending
