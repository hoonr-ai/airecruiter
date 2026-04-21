"""Shared helpers for route modules.

Re-exports the canonical DB connection from core.db so router modules
have one import path.
"""

from core.db import get_db_connection, get_dict_cursor_connection

__all__ = ["get_db_connection", "get_dict_cursor_connection"]
