"""Tira chat service.

Wraps gpt-4o-mini with OpenAI tool-calling so Tira can answer questions about
live Hoonr state (job status, candidate counts, portfolio snapshot) by calling
typed tools backed by direct SQL. No vector DB, no RAG — just narrow,
deterministic lookups.
"""
import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from openai import AsyncOpenAI

from core.config import DATABASE_URL, OPENAI_API_KEY

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are Tira, a recruiting sidekick inside Hoonr. You help recruiters find "
    "candidates, shape job rubrics, score resumes, and move submissions forward. "
    "Keep replies short, specific, and action-oriented. Refer to the product as "
    "Hoonr.\n\n"
    "When a user asks about a specific job (e.g. 'status of 26-12345', 'how many "
    "candidates on job 32129274', 'what's happening with the X-Ray role'), call "
    "the get_job_status tool. When they ask 'what jobs are active', 'recent "
    "jobs', 'my portfolio', call list_recent_jobs. Prefer calling a tool over "
    "guessing — never fabricate counts or statuses.\n\n"
    "When pointing to features, name them: the Tira panel has Chat, Boolean, "
    "Resume match, and Report bug modes."
)


# ---------------------------------------------------------------------------
# Tool schemas sent to the model
# ---------------------------------------------------------------------------

_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_job_status",
            "description": (
                "Look up a single job in Hoonr by JobDiva ref code (e.g. '26-12345') "
                "or numeric job ID. Returns title, customer, status, archive flag, "
                "openings, allowed submittals, and live candidate counts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_ref": {
                        "type": "string",
                        "description": "JobDiva ref code like '26-12345' or numeric job ID.",
                    },
                },
                "required": ["job_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_jobs",
            "description": (
                "List the most recently updated jobs in Hoonr's portfolio with "
                "their candidate counts. Use for 'my jobs', 'recent jobs', "
                "'active jobs', 'portfolio overview'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many jobs to return (default 10, max 25).",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "Include archived jobs (default false).",
                    },
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _row_to_job_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(row)
    for k in ("created_at", "updated_at"):
        if d.get(k) and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    return d


def _tool_get_job_status(job_ref: str) -> Dict[str, Any]:
    ref = (job_ref or "").strip()
    if not ref:
        return {"found": False, "error": "empty job_ref"}
    try:
        with psycopg2.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT mj.job_id, mj.jobdiva_id, mj.title, mj.customer_name,
                           mj.status, mj.city, mj.state, mj.openings,
                           mj.max_allowed_submittals, mj.is_archived,
                           mj.created_at, mj.updated_at,
                           COALESCE(sc.total, 0)        AS candidates_sourced,
                           COALESCE(sc.shortlisted, 0)  AS resumes_shortlisted
                    FROM monitored_jobs mj
                    LEFT JOIN (
                        SELECT mj2.job_id AS mj_job_id,
                               COUNT(*)                                                 AS total,
                               COUNT(*) FILTER (WHERE sc2.resume_match_percentage >= 70) AS shortlisted
                        FROM sourced_candidates sc2
                        JOIN monitored_jobs mj2
                          ON sc2.jobdiva_id = mj2.jobdiva_id
                          OR sc2.jobdiva_id = mj2.job_id::text
                        GROUP BY mj2.job_id
                    ) sc ON sc.mj_job_id = mj.job_id
                    WHERE mj.jobdiva_id = %s OR mj.job_id::text = %s
                    LIMIT 1
                    """,
                    (ref, ref),
                )
                row = cur.fetchone()
                if not row:
                    return {"found": False, "job_ref": ref}
                return {"found": True, **_row_to_job_dict(row)}
    except Exception as e:
        logger.error(f"get_job_status({ref}) failed: {e}")
        return {"found": False, "error": str(e), "job_ref": ref}


def _tool_list_recent_jobs(limit: int = 10, include_archived: bool = False) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 10), 25))
    try:
        with psycopg2.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT mj.job_id, mj.jobdiva_id, mj.title, mj.customer_name,
                           mj.status, mj.is_archived, mj.updated_at,
                           COALESCE(sc.total, 0)       AS candidates_sourced,
                           COALESCE(sc.shortlisted, 0) AS resumes_shortlisted
                    FROM monitored_jobs mj
                    LEFT JOIN (
                        SELECT mj2.job_id AS mj_job_id,
                               COUNT(*)                                                 AS total,
                               COUNT(*) FILTER (WHERE sc2.resume_match_percentage >= 70) AS shortlisted
                        FROM sourced_candidates sc2
                        JOIN monitored_jobs mj2
                          ON sc2.jobdiva_id = mj2.jobdiva_id
                          OR sc2.jobdiva_id = mj2.job_id::text
                        GROUP BY mj2.job_id
                    ) sc ON sc.mj_job_id = mj.job_id
                    WHERE (%s OR mj.is_archived IS NOT TRUE)
                    ORDER BY mj.updated_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    (bool(include_archived), limit),
                )
                rows = cur.fetchall() or []
                return {
                    "count": len(rows),
                    "jobs": [_row_to_job_dict(r) for r in rows],
                }
    except Exception as e:
        logger.error(f"list_recent_jobs failed: {e}")
        return {"count": 0, "jobs": [], "error": str(e)}


_TOOL_IMPLS = {
    "get_job_status": lambda args: _tool_get_job_status(args.get("job_ref", "")),
    "list_recent_jobs": lambda args: _tool_list_recent_jobs(
        limit=args.get("limit", 10),
        include_archived=args.get("include_archived", False),
    ),
}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ChatService:
    """Async chat with tool-calling. One round of tool execution per turn
    (sufficient for the single-lookup questions Tira handles today)."""

    def __init__(self):
        self.client: Optional[AsyncOpenAI] = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

    async def get_response(self, message: str, history: List[Any]) -> str:
        if not self.client:
            return "I'm Tira, your recruiting sidekick. (Mock Mode: OpenAI Key missing)"

        try:
            messages: List[Dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
            # `history` is a list of pydantic Message models — both .role and .content exist.
            for h in history:
                role = getattr(h, "role", None) or (h.get("role") if isinstance(h, dict) else None)
                content = getattr(h, "content", None) or (h.get("content") if isinstance(h, dict) else None)
                if role and content:
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": message})

            # Round 1: let the model decide whether to call a tool.
            first = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
            )
            choice = first.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None) or []

            if not tool_calls:
                return choice.content or ""

            # Execute each tool call and feed results back.
            messages.append(
                {
                    "role": "assistant",
                    "content": choice.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                impl = _TOOL_IMPLS.get(name)
                result = impl(args) if impl else {"error": f"Unknown tool {name}"}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str),
                    }
                )

            # Round 2: let the model answer with tool results in context.
            second = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
            )
            return second.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Tira chat failed: {e}")
            return f"I'm having trouble connecting to my brain right now. ({e})"


chat_service = ChatService()
