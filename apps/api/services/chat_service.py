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
from core.db import get_dict_cursor_connection
from core.llm_client import get_openai_client

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


def _candidate_counts_by_jobdiva_id(cur, keys: List[str]) -> Dict[str, Dict[str, int]]:
    """One indexed lookup for total/shortlisted candidate counts across a set
    of jobdiva_id values. Replaces the pre-fix `COUNT(*) ... GROUP BY mj2.job_id`
    subquery whose `OR sc2.jobdiva_id = mj2.job_id::text` join forced a seq
    scan on all of sourced_candidates per chat message.

    Pass both alphanumeric `jobdiva_id` and `job_id::text` strings — rows for
    one logical job may have been stored under either form. The caller sums
    them per job.
    """
    if not keys:
        return {}
    cur.execute(
        """
        SELECT jobdiva_id,
               COUNT(*)                                                  AS total,
               COUNT(*) FILTER (WHERE resume_match_percentage >= 70)     AS shortlisted
        FROM sourced_candidates
        WHERE jobdiva_id = ANY(%s)
        GROUP BY jobdiva_id
        """,
        (list({k for k in keys if k}),),
    )
    return {
        r["jobdiva_id"]: {"total": int(r["total"]), "shortlisted": int(r["shortlisted"])}
        for r in (cur.fetchall() or [])
    }


def _sum_counts_for_job(counts: Dict[str, Dict[str, int]], job: Dict[str, Any]) -> Dict[str, int]:
    total = 0
    shortlisted = 0
    for key in (job.get("jobdiva_id"), str(job["job_id"]) if job.get("job_id") is not None else None):
        if key and key in counts:
            total += counts[key]["total"]
            shortlisted += counts[key]["shortlisted"]
    return {"total": total, "shortlisted": shortlisted}


def _tool_get_job_status(job_ref: str) -> Dict[str, Any]:
    ref = (job_ref or "").strip()
    if not ref:
        return {"found": False, "error": "empty job_ref"}
    try:
        with get_dict_cursor_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1. Job lookup — covered by idx_monitored_jobs_jobdiva_id /
                # idx_monitored_jobs_job_id.
                cur.execute(
                    """
                    SELECT job_id, jobdiva_id, title, customer_name,
                           status, city, state, openings,
                           max_allowed_submittals, is_archived,
                           created_at, updated_at
                    FROM monitored_jobs
                    WHERE jobdiva_id = %s OR job_id::text = %s
                    LIMIT 1
                    """,
                    (ref, ref),
                )
                job = cur.fetchone()
                if not job:
                    return {"found": False, "job_ref": ref}

                # 2. Bounded count — single index scan on jobdiva_id, no JOIN.
                counts = _candidate_counts_by_jobdiva_id(
                    cur,
                    [job.get("jobdiva_id"), str(job["job_id"]) if job.get("job_id") is not None else None],
                )
                summed = _sum_counts_for_job(counts, job)
                enriched = {
                    **dict(job),
                    "candidates_sourced": summed["total"],
                    "resumes_shortlisted": summed["shortlisted"],
                }
                return {"found": True, **_row_to_job_dict(enriched)}
    except Exception as e:
        logger.error(f"get_job_status({ref}) failed: {e}")
        return {"found": False, "error": str(e), "job_ref": ref}


def _tool_list_recent_jobs(limit: int = 10, include_archived: bool = False) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 10), 25))
    try:
        with get_dict_cursor_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1. Cheap monitored_jobs scan, bounded by LIMIT.
                cur.execute(
                    """
                    SELECT job_id, jobdiva_id, title, customer_name,
                           status, is_archived, updated_at
                    FROM monitored_jobs
                    WHERE (%s OR is_archived IS NOT TRUE)
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT %s
                    """,
                    (bool(include_archived), limit),
                )
                jobs = cur.fetchall() or []
                if not jobs:
                    return {"count": 0, "jobs": []}

                # 2. Single GROUP BY scoped to the small set of keys we care
                # about. With idx_sourced_candidates_jobdiva_id this is a
                # bounded index scan, not a full-table aggregate.
                keys: List[str] = []
                for j in jobs:
                    if j.get("jobdiva_id"):
                        keys.append(j["jobdiva_id"])
                    if j.get("job_id") is not None:
                        keys.append(str(j["job_id"]))
                counts = _candidate_counts_by_jobdiva_id(cur, keys)

                enriched_jobs = []
                for j in jobs:
                    summed = _sum_counts_for_job(counts, j)
                    enriched_jobs.append(_row_to_job_dict({
                        **dict(j),
                        "candidates_sourced": summed["total"],
                        "resumes_shortlisted": summed["shortlisted"],
                    }))
                return {"count": len(enriched_jobs), "jobs": enriched_jobs}
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
        self.client: Optional[AsyncOpenAI] = get_openai_client()

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
            # `prompt_cache_key` lets OpenAI's automatic prefix cache route
            # repeat traffic for the (system + tools) preamble through the
            # same cache shard, halving input-token cost on the cached
            # prefix once a conversation grows past ~1024 tokens.
            first = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
                prompt_cache_key="tira-chat-v1",
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
                prompt_cache_key="tira-chat-v1",
            )
            return second.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Tira chat failed: {e}")
            return f"I'm having trouble connecting to my brain right now. ({e})"


chat_service = ChatService()
