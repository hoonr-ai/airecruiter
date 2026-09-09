import httpx
import json
import logging
import asyncio
import re
import time
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional
from core import (
    UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID, UNIPILE_ACCOUNT_IDS
)

logger = logging.getLogger(__name__)

# How long a discovered-accounts listing stays fresh before we re-hit
# GET /accounts. Attaching a new LinkedIn account to the Unipile workspace
# is picked up within this window without a restart.
_ACCOUNTS_CACHE_TTL_S = 300

# Cooldown applied to an account after an account-level failure, by class.
_COOLDOWN_AUTH_S = 30 * 60      # 401/403/checkpoint — needs human attention
_COOLDOWN_RATE_LIMIT_S = 15 * 60  # 429 — LinkedIn throttled this account
_COOLDOWN_TRANSIENT_S = 5 * 60  # 5xx — brief backoff, likely recovers


class UnipileService:
    def __init__(self):
        # Use centralized config
        dsn = UNIPILE_DSN
        if not dsn.startswith("http"):
            dsn = f"https://{dsn}"
        self.api_url = f"{dsn}/api/v1"

        self.api_key = UNIPILE_API_KEY
        # Legacy single-account id, kept for callers that read .account_id
        # directly and as a discovery-outage fallback. NOT a rotation pin.
        self.account_id = UNIPILE_ACCOUNT_ID or (UNIPILE_ACCOUNT_IDS[0] if UNIPILE_ACCOUNT_IDS else "")
        # Explicit pin list (UNIPILE_ACCOUNT_IDS env only). Empty = rotate
        # across every discovered workspace account.
        self.pinned_account_ids = list(UNIPILE_ACCOUNT_IDS)
        # Used only when the /accounts listing is unreachable and no cache exists.
        self.fallback_account_ids = list(UNIPILE_ACCOUNT_IDS) or ([UNIPILE_ACCOUNT_ID] if UNIPILE_ACCOUNT_ID else [])
        self._id_cache = {} # Simple in-memory cache for skill/location IDs
        # Discovered LinkedIn accounts: [{"id","name","status"}], cached per worker.
        self._accounts_cache: List[Dict[str, Any]] = []
        self._accounts_cache_at: float = 0.0
        self._usage_table_ready = False
        # In-process fallback pointer when the DB round-robin is unavailable.
        self._local_rr_idx = 0
        # account_id -> monotonic time when Unipile answered the Recruiter
        # search with 403 errors/feature_not_subscribed (no Recruiter seat).
        # Such accounts go straight to LinkedIn *classic* people search
        # instead of burning a Recruiter attempt (and a 30-min bench) every
        # search. Expires after UNIPILE_NO_RECRUITER_TTL_S so a newly bought
        # seat is picked up without a restart.
        self._recruiter_unavailable: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Multi-account discovery + round-robin rotation
    # ------------------------------------------------------------------
    async def list_linkedin_accounts(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """All LinkedIn accounts attached to the Unipile workspace.

        Returns [{"id", "name", "status"}]. Cached for _ACCOUNTS_CACHE_TTL_S.
        Falls back to the configured id list (status unknown) when the
        listing call fails, so an API blip doesn't kill the channel.
        """
        if not self.api_key:
            return []

        now = time.monotonic()
        if not force_refresh and self._accounts_cache and (now - self._accounts_cache_at) < _ACCOUNTS_CACHE_TTL_S:
            return self._accounts_cache

        url = f"{self.api_url}/accounts"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    accounts = []
                    for acc in raw:
                        if str(acc.get("type", "")).upper() != "LINKEDIN" or not acc.get("id"):
                            continue
                        # Health lives under sources[].status on current Unipile
                        # API versions; older payloads had a top-level status.
                        status = str(acc.get("status") or "").upper()
                        if not status:
                            source_statuses = [
                                str(s.get("status") or "").upper()
                                for s in (acc.get("sources") or [])
                                if isinstance(s, dict)
                            ]
                            if source_statuses:
                                status = ("OK" if all(s == "OK" for s in source_statuses)
                                          else next(s for s in source_statuses if s != "OK"))
                        accounts.append({
                            "id": acc.get("id"),
                            "name": acc.get("name") or "",
                            "status": status,
                        })
                    if accounts:
                        self._accounts_cache = accounts
                        self._accounts_cache_at = now
                        return accounts
                    logger.warning("Unipile: no LinkedIn accounts attached to the workspace.")
                else:
                    logger.error(f"Unipile Accounts Error: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Unipile Account Fetch Exception: {e}")

        if self._accounts_cache:
            return self._accounts_cache  # stale beats empty
        return [{"id": a, "name": "", "status": ""} for a in self.fallback_account_ids]

    async def get_rotation_account_ids(self) -> List[str]:
        """Account ids eligible for rotation.

        Auto-discovery is primary so newly attached accounts join the pool
        without a config change. Only the explicit UNIPILE_ACCOUNT_IDS env
        var pins rotation to a subset — the legacy UNIPILE_ACCOUNT_ID never
        pins (every old deployment has it set, and honoring it would silently
        reduce the pool back to one account).
        """
        accounts = await self.list_linkedin_accounts()
        healthy = [a["id"] for a in accounts if a.get("status") in ("OK", "")]
        discovered = healthy or [a["id"] for a in accounts]
        if self.pinned_account_ids:
            pinned = [a for a in discovered if a in self.pinned_account_ids]
            return pinned or self.pinned_account_ids
        return discovered

    def _ensure_usage_table_sync(self) -> None:
        from core.db import get_db_connection
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Advisory lock: 8 workers × concurrent coroutines can race
                # here on first use, and CREATE TABLE IF NOT EXISTS is not
                # concurrency-safe (losers raise 42P07/23505 on the catalog).
                # TIMESTAMPTZ so serialized values carry an explicit offset —
                # the frontend parses them with new Date().
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('unipile_account_usage_ddl'))")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS unipile_account_usage (
                        account_id TEXT PRIMARY KEY,
                        account_name TEXT,
                        use_count BIGINT NOT NULL DEFAULT 0,
                        last_used_at TIMESTAMPTZ NULL,
                        cooldown_until TIMESTAMPTZ NULL,
                        last_error TEXT,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                # Which LinkedIn search API the account last succeeded with
                # ("recruiter" | "classic") — lets the admin page say that an
                # account is healthy but seat-less instead of "In rotation"
                # with a stale 403 underneath.
                cur.execute("ALTER TABLE unipile_account_usage ADD COLUMN IF NOT EXISTS search_api TEXT")
            conn.commit()
        except Exception as e:
            # A concurrent creator winning the race is success, not failure.
            if "already exists" not in str(e) and "duplicate key" not in str(e):
                raise
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

    def _acquire_account_sync(self, account_ids: List[str], names: Dict[str, str]) -> Optional[str]:
        """Atomically claim the least-recently-used eligible account.

        The UPDATE...RETURNING with FOR UPDATE SKIP LOCKED makes the claim
        safe across the 8 uvicorn workers — two concurrent searches can't
        both bump the same account, so usage spreads round-robin cluster-wide.
        """
        from core.db import get_db_connection
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # DO NOTHING (not DO UPDATE): an update arm would row-lock
                # every existing account until commit, serializing all claims
                # across workers and inviting lock-order deadlocks. Sorted
                # order keeps insertion of genuinely-new rows deadlock-free.
                for aid in sorted(account_ids):
                    cur.execute("""
                        INSERT INTO unipile_account_usage (account_id, account_name)
                        VALUES (%s, %s)
                        ON CONFLICT (account_id) DO NOTHING
                    """, (aid, names.get(aid, "")))
                claim_sql = """
                    UPDATE unipile_account_usage
                    SET use_count = use_count + 1, last_used_at = NOW(), updated_at = NOW()
                    WHERE account_id = (
                        SELECT account_id FROM unipile_account_usage
                        WHERE account_id = ANY(%s) {cooldown_clause}
                        ORDER BY last_used_at ASC NULLS FIRST, account_id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING account_id
                """
                cur.execute(
                    claim_sql.format(cooldown_clause="AND (cooldown_until IS NULL OR cooldown_until <= NOW())"),
                    (account_ids,),
                )
                row = cur.fetchone()
                if not row:
                    # Every account is cooling down (or lock-contended) —
                    # a degraded search beats no search: claim LRU anyway.
                    cur.execute(claim_sql.format(cooldown_clause=""), (account_ids,))
                    row = cur.fetchone()
            conn.commit()
            # Refresh display names in their own short transaction, outside
            # the claim, so name churn never holds locks during a claim.
            try:
                with conn.cursor() as cur:
                    for aid, name in names.items():
                        if name:
                            cur.execute("""
                                UPDATE unipile_account_usage
                                SET account_name = %s, updated_at = NOW()
                                WHERE account_id = %s
                                  AND account_name IS DISTINCT FROM %s
                            """, (name, aid, name))
                conn.commit()
            except Exception:
                conn.rollback()
            return row[0] if row else None
        finally:
            conn.close()

    def _mark_account_failure_sync(self, account_id: str, error: str, cooldown_s: int) -> None:
        from core.db import get_db_connection
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Upsert: the row may not exist yet (e.g. the account was
                # never claimed through the DB path) — a bare UPDATE would
                # silently no-op and the bench would never stick.
                cur.execute("""
                    INSERT INTO unipile_account_usage (account_id, cooldown_until, last_error, updated_at)
                    VALUES (%s, NOW() + (%s || ' seconds')::interval, %s, NOW())
                    ON CONFLICT (account_id) DO UPDATE
                    SET cooldown_until = EXCLUDED.cooldown_until,
                        last_error = EXCLUDED.last_error,
                        updated_at = NOW()
                """, (account_id, str(int(cooldown_s)), error[:500]))
            conn.commit()
        finally:
            conn.close()

    async def acquire_account(self) -> Optional[str]:
        """Pick the next LinkedIn account, round-robin across the cluster.

        Single-account pools still go through the DB claim so the admin
        dashboard sees usage counts and benching telemetry for that account.
        """
        account_ids = await self.get_rotation_account_ids()
        if not account_ids:
            logger.warning("Unipile: no LinkedIn accounts available (none attached / none configured).")
            return None

        names = {a["id"]: a.get("name") or "" for a in (self._accounts_cache or [])}
        try:
            if not self._usage_table_ready:
                await asyncio.to_thread(self._ensure_usage_table_sync)
                self._usage_table_ready = True
            claimed = await asyncio.to_thread(self._acquire_account_sync, account_ids, names)
            if claimed:
                if len(account_ids) > 1:
                    logger.info(f"Unipile: rotated to LinkedIn account {claimed}")
                return claimed
        except Exception as e:
            logger.warning(f"Unipile: DB round-robin unavailable ({e}); using in-process rotation.")

        # Fallback: per-worker cycle. Not cluster-fair, but never blocks a search.
        idx = self._local_rr_idx % len(account_ids)
        self._local_rr_idx = idx + 1
        return account_ids[idx]

    def _mark_account_success_sync(self, account_id: str, search_api: str) -> None:
        from core.db import get_db_connection
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Conditional so the common case (already clean) is a no-op
                # read, not a write on every search.
                cur.execute("""
                    UPDATE unipile_account_usage
                    SET last_error = NULL, cooldown_until = NULL,
                        search_api = %s, updated_at = NOW()
                    WHERE account_id = %s
                      AND (last_error IS NOT NULL OR cooldown_until IS NOT NULL
                           OR search_api IS DISTINCT FROM %s)
                """, (search_api, account_id, search_api))
            conn.commit()
        finally:
            conn.close()

    async def mark_account_success(self, account_id: str, search_api: str) -> None:
        """A search on this account returned rows: clear its stale error /
        cooldown and record which API worked. Without this, `last_error`
        was sticky forever — the admin page showed month-old failures on
        accounts that had been fine since."""
        try:
            if not self._usage_table_ready:
                await asyncio.to_thread(self._ensure_usage_table_sync)
                self._usage_table_ready = True
            await asyncio.to_thread(self._mark_account_success_sync, account_id, search_api)
        except Exception as e:
            logger.debug(f"Unipile: failed to record account success: {e}")

    async def mark_account_failure(self, account_id: str, error: str, cooldown_s: int) -> None:
        """Bench a misbehaving account so rotation skips it for a while."""
        logger.error(f"Unipile: benching account {account_id} for {cooldown_s}s — {error[:200]}")
        try:
            if not self._usage_table_ready:
                await asyncio.to_thread(self._ensure_usage_table_sync)
                self._usage_table_ready = True
            await asyncio.to_thread(self._mark_account_failure_sync, account_id, error, cooldown_s)
        except Exception as e:
            logger.warning(f"Unipile: failed to persist account cooldown: {e}")

    @staticmethod
    def classify_account_failure(status_code: int, body: str) -> Optional[int]:
        """Cooldown seconds if the failure is account-level, else None."""
        text = (body or "").lower()
        # Unipile error bodies carry a `type` such as
        # errors/disconnected_account, errors/multiple_sessions (LinkedIn
        # logged the seat out because it was opened elsewhere) or
        # errors/insufficient_credentials — all need a human to reconnect
        # the account in the Unipile dashboard, so bench for the long window.
        if status_code in (401, 403) or any(
            marker in text
            for marker in (
                "checkpoint", "disconnected", "credentials", "expired",
                "invalid account", "multiple_session",
            )
        ):
            return _COOLDOWN_AUTH_S
        if status_code == 429 or "rate limit" in text or "too many" in text:
            return _COOLDOWN_RATE_LIMIT_S
        # 5xx incl. 504 errors/request_timeout (LinkedIn upstream too slow)
        # and 500 errors/unexpected_error — Unipile-side, usually recovers.
        if status_code >= 500 or "request_timeout" in text:
            return _COOLDOWN_TRANSIENT_S
        # 404/422 can be account-scoped (e.g. a seat without Recruiter access,
        # or an account-specific resource) — bench briefly and let the caller
        # try a sibling. A genuinely bad payload fails on every sibling and
        # still ends as an empty result, so rotating costs little; treating
        # these as fatal used to zero the whole channel on one bad account.
        if status_code in (404, 422):
            return _COOLDOWN_TRANSIENT_S
        return None

    def _clean_candidate_name(self, value: Optional[str]) -> Optional[str]:
        raw = re.sub(r"\s+", " ", str(value or "")).strip()
        if not raw:
            return None

        normalized = raw.casefold()
        placeholders = {
            "linkedin candidate",
            "professional candidate",
            "unknown candidate",
            "candidate",
            "unknown",
        }
        if normalized in placeholders:
            return None
            
        # Alphanumeric ID detection: Reject long strings with no spaces that contain digits
        # e.g. "Aemaaesrdj8Bputbeeugzft99J0Qcie7Kbhun5K"
        if len(raw) > 15 and " " not in raw:
            if any(c.isdigit() for c in raw):
                return None
            # Also reject if it has extremely suspicious character distribution (e.g. hashes)
            if len(re.findall(r'[A-Z]', raw)) > 5 and len(re.findall(r'[a-z]', raw)) > 5:
                return None

        return raw

    def _derive_name_from_profile_url(self, profile_url: Optional[str]) -> Optional[str]:
        if not profile_url:
            return None

        try:
            parsed = urlparse(profile_url)
        except Exception:
            return None

        slug = ""
        path_parts = [part for part in (parsed.path or "").split("/") if part]
        if "in" in path_parts:
            in_index = path_parts.index("in")
            if in_index + 1 < len(path_parts):
                slug = path_parts[in_index + 1]
        elif path_parts:
            slug = path_parts[-1]

        slug = re.sub(r"[-_]+", " ", slug).strip()
        slug = re.sub(r"\b\d+\b", " ", slug)
        slug = re.sub(r"\s+", " ", slug).strip()
        if not slug:
            return None

        if not re.search(r"[a-zA-Z]{2,}", slug):
            return None

        candidate_name = " ".join(part.capitalize() for part in slug.split()[:4])
        return self._clean_candidate_name(candidate_name)

    def _resolve_candidate_name(self, item: Dict[str, Any]) -> str:
        # Try multiple fallbacks before using generic name
        explicit_name = self._clean_candidate_name(item.get("name"))
        if explicit_name:
            return explicit_name

        # Try first_name + last_name from profile
        first_name = self._clean_candidate_name(item.get("first_name") or item.get("firstName"))
        last_name = self._clean_candidate_name(item.get("last_name") or item.get("lastName"))
        if first_name or last_name:
            return f"{first_name} {last_name}".strip()

        # Only the PUBLIC vanity URL carries a name slug. The recruiter-mode
        # `profile_url` is `/talent/search/profile/<AEMAA… hash>`, which used
        # to be fed here and rendered opaque hashes as candidate names.
        derived_name = self._derive_name_from_profile_url(self._public_profile_url(item))
        if derived_name:
            return derived_name

        # Try using headline as fallback
        headline = self._clean_candidate_name(item.get("headline"))
        if headline:
            return headline
        
        # Try using current company or title
        company = item.get("company") or item.get("current_company")
        title = item.get("title") or item.get("current_title")
        if company or title:
            return f"{title or 'Professional'} at {company or 'Company'}".strip()

        # Last resort - use provider ID to make it unique
        provider_id = item.get("id") or item.get("provider_id")
        if provider_id:
            return f"LinkedIn Professional {str(provider_id)[:8]}"

        return "LinkedIn Candidate"

    @staticmethod
    def _is_public_linkedin_url(url: Optional[str]) -> bool:
        return "linkedin.com/in/" in str(url or "").strip().lower()

    def _public_profile_url(self, item: Dict[str, Any]) -> Optional[str]:
        """Public vanity URL (``linkedin.com/in/<slug>``) for a search or
        profile row, or None when the row has none (~2% of recruiter rows).

        Recruiter-mode rows put it under ``public_profile_url`` (or expose
        just the slug as ``public_identifier``); their ``profile_url`` is a
        Recruiter deep link that needs an RPS seat and is never returned
        here — see :py:meth:`_recruiter_profile_url`.
        """
        if not isinstance(item, dict):
            return None
        for key in ("public_profile_url", "public_url"):
            url = str(item.get(key) or "").strip()
            if self._is_public_linkedin_url(url):
                return url
        ident = str(item.get("public_identifier") or "").strip().strip("/")
        if ident and "/" not in ident and " " not in ident:
            return f"https://www.linkedin.com/in/{ident}"
        url = str(item.get("profile_url") or "").strip()
        if self._is_public_linkedin_url(url):
            return url
        return None

    def _recruiter_profile_url(self, item: Dict[str, Any]) -> Optional[str]:
        """The RPS-only Recruiter deep link, when the row's ``profile_url``
        is one (i.e. not a public ``/in/`` URL)."""
        if not isinstance(item, dict):
            return None
        url = str(item.get("profile_url") or "").strip()
        if url and not self._is_public_linkedin_url(url):
            return url
        return None

    def _split_candidate_name(self, full_name: str) -> tuple[str, str]:
        cleaned = re.sub(r"\s+", " ", str(full_name or "")).strip()
        if not cleaned:
            return "", ""
        parts = cleaned.split(" ", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    def _get_headers(self):
        return {
            "X-API-KEY": self.api_key,
            "Accept": "application/json"
        }

    async def _resolve_id(self, category: str, name: str, account_id: Optional[str] = None) -> Optional[str]:
        """Resolves a string name to a LinkedIn ID (Geurn) using Unipile endpoints."""
        cache_key = f"{category}:{name.lower()}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]

        account_id = account_id or await self.acquire_account()
        if not account_id: return None

        # Fixed endpoint: /linkedin/search/parameters instead of /linkedin/search/skills 
        # which was returning 404 in the logs.
        url = f"{self.api_url}/linkedin/search/parameters"
        p_type = "SKILL" if category == "skill" else "LOCATION"
        params = {"account_id": account_id, "keywords": name, "type": p_type}
        
        try:
             async with httpx.AsyncClient(timeout=10.0) as client:
                 resp = await client.get(url, params=params, headers=self._get_headers())
                 if resp.status_code == 200:
                     items = resp.json().get("items", [])
                     if items:
                         # IMPROVEDish: Find the best match in the returned list
                         # Unipile parameters list might return many matches
                         best_match = items[0]
                         for item in items:
                             if item.get("title", "").lower() == name.lower():
                                 best_match = item
                                 break
                         
                         res_id = best_match.get("id")
                         self._id_cache[cache_key] = res_id
                         return res_id
                 else:
                     logger.warning(f"Unipile: Parameter resolution returned {resp.status_code} for {category} '{name}'")
        except Exception as e:
            logger.error(f"Unipile: ID resolution failed for {category} '{name}': {e}")
        return None

    async def get_account_id(self) -> Optional[str]:
        """Legacy single-account accessor — now a rotation claim.

        Kept for backward compatibility; new code should call
        acquire_account() (rotation) or list_linkedin_accounts() (inventory).
        """
        if not self.api_key:
            logger.warning("Unipile API Key is missing.")
            return None
        return await self.acquire_account()

    def _sanitize_linkedin_keywords(
        self,
        boolean_string: str,
        resolved_skill_names: List[str],
    ) -> str:
        s = boolean_string or ""
        # Drop years-of-experience phrases — LinkedIn profiles rarely contain the exact "10+ years" literal
        s = re.sub(r'"\d+\+\s*years?"', "", s, flags=re.IGNORECASE)
        # Drop JobDiva-dialect experience clauses ("OVER 5 YRS") — the wizard
        # builds one boolean for all ticked sources and keys its dialect on
        # JobDiva, so these tokens leak into LinkedIn searches and match nothing.
        s = re.sub(r'\bOVER\s+\d+\s+YRS?\b', "", s, flags=re.IGNORECASE)
        s = re.sub(r'\s+AND\s+recent', "", s, flags=re.IGNORECASE)
        # Drop location radius clauses UNCONDITIONALLY. These were only
        # stripped when the location resolved to a geo ID — exactly backwards:
        # when resolution FAILED, the literal `"Dallas, TX" within 25 mi`
        # stayed in the keyword string, and LinkedIn keyword search ANDs it
        # against profile bodies, collapsing results to near-zero. A location
        # literal never helps keyword search; geo filtering rides on the
        # structured location URN plus our post-search gates.
        s = re.sub(r'"[^"]+"\s+within\s+\d+\s+mi', "", s, flags=re.IGNORECASE)
        s = re.sub(r'within\s+\d+\s+mi', "", s, flags=re.IGNORECASE)
        # Same class of failure: a literal country phrase ANDed into keywords
        # (historic server-side scoping, or a wizard-authored clause) matches
        # almost no profile body. Strip the known spellings.
        s = re.sub(r'\s+AND\s+"(?:United States|United States of America|USA|U\.S\.A?\.?|Canada)"', "", s, flags=re.IGNORECASE)
        s = re.sub(r'"(?:United States|United States of America|USA|U\.S\.A?\.?|Canada)"', "", s, flags=re.IGNORECASE)
        # Drop quoted skill terms we've already resolved to IDs
        for name in resolved_skill_names:
            escaped = re.escape(name)
            s = re.sub(rf'"{escaped}"', "", s, flags=re.IGNORECASE)
        # Clean up leftover connectives / empty parens
        for _ in range(6):
            s = re.sub(r'\(\s*\)', "", s)
            s = re.sub(r'\(\s*(AND|OR|NOT)\s+', "(", s, flags=re.IGNORECASE)
            s = re.sub(r'\s+(AND|OR|NOT)\s*\)', ")", s, flags=re.IGNORECASE)
            s = re.sub(r'\s+(AND|OR)\s+(AND|OR)\s+', r" \1 ", s, flags=re.IGNORECASE)
            s = re.sub(r'^\s*(AND|OR|NOT)\s+', "", s, flags=re.IGNORECASE)
            s = re.sub(r'\s+(AND|OR|NOT)\s*$', "", s, flags=re.IGNORECASE)
        s = re.sub(r'\s+', " ", s).strip()
        # Balance parentheses: drop any ')' without a matching '(', append missing closers
        balanced = []
        depth = 0
        for ch in s:
            if ch == ')':
                if depth == 0:
                    continue
                depth -= 1
            elif ch == '(':
                depth += 1
            balanced.append(ch)
        s = "".join(balanced) + (")" * depth)
        s = re.sub(r'\s+', " ", s).strip()
        # Unwrap a single outer parenthesis only if the opening '(' truly matches the closing ')'
        if s.startswith("(") and s.endswith(")"):
            depth = 0
            wraps_all = True
            for i, ch in enumerate(s):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if depth == 0 and i < len(s) - 1:
                    wraps_all = False
                    break
            if wraps_all:
                s = s[1:-1].strip()
        return s

    async def search_candidates(self, skills: List[Any], location: str, open_to_work: bool = True, limit: int = 25, boolean_string: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search LinkedIn via Unipile using the Recruiter API mode.

        Each search claims the least-recently-used attached LinkedIn account
        (cluster-wide round-robin), so volume spreads evenly instead of
        hammering one account.

        Rotation-on-failure — the search moves to a sibling account when the
        claimed one:
          - fails with an account-level error (401/403 checkpoint/expired,
            429 rate-limit, 5xx) — the account is benched, then a sibling
            is tried;
          - hits a network error / timeout — a sibling is tried WITHOUT
            benching (a per-session hang isn't the account's fault);
          - returns HTTP 200 but ZERO candidates — a sibling is tried, so a
            silently-degraded account (e.g. a lost Recruiter seat that returns
            empty) doesn't sink the whole search. Rotation continues through
            every sibling until one yields candidates or the pool is exhausted.

        The only non-rotating failure is a bad-payload 4xx (e.g. 400/422):
        every account would reject an identical request, so siblings aren't
        burned on it.
        """
        # LinkedIn Recruiter searches are capped per-search to protect the
        # attached accounts (rate/abuse limits) — 100 per search, tunable.
        try:
            from core import sourcing_config as _sc
            _cap = int(getattr(_sc, "UNIPILE_SEARCH_LIMIT", 100) or 100)
        except Exception:
            _cap = 100
        limit = max(1, min(int(limit or 25), _cap))

        # Cap attempts at the size of the rotation pool: `acquire_account`
        # hands back the LRU account each call, and the `tried` guard stops us
        # once it cycles back to one we've already searched (pool exhausted).
        rotation_ids = await self.get_rotation_account_ids()
        max_attempts = max(1, len(rotation_ids))

        tried: set = set()
        for _ in range(max_attempts):
            account_id = await self.acquire_account()
            if not account_id:
                # No account available at all.
                break
            if account_id in tried:
                # Under multi-worker SKIP LOCKED contention the LRU claim can
                # hand back an id we already searched while an untried sibling
                # exists — burn this attempt and re-claim instead of aborting
                # the whole rotation (max_attempts bounds the loop).
                continue
            tried.add(account_id)
            search_api = "recruiter"
            if self._recruiter_known_unavailable(account_id):
                # Seat-less account (remembered from an earlier 403
                # errors/feature_not_subscribed): classic search directly.
                search_api = "classic"
                results, outcome = await self._search_classic_once(
                    account_id, skills, location, limit, boolean_string
                )
            else:
                results, outcome = await self._search_candidates_once(
                    account_id, skills, location, open_to_work, limit, boolean_string
                )
                if outcome == "classic":
                    # Unipile just told us this account has no Recruiter
                    # seat — fall back to classic people search on the SAME
                    # account rather than benching a perfectly usable seat.
                    search_api = "classic"
                    results, outcome = await self._search_classic_once(
                        account_id, skills, location, limit, boolean_string
                    )
            if outcome == "fatal":
                # Bad-payload 4xx — a sibling would reject it identically.
                return results
            if outcome == "ok" and results:
                await self.mark_account_success(account_id, search_api)
                return results
            # "ok" but empty, or "rotate" (benched account-level error, or a
            # non-benched network blip): try the next sibling.
            logger.info(
                "Unipile: account %s yielded no candidates (%s); rotating to a "
                "sibling (%d/%d accounts tried).",
                account_id, outcome, len(tried), max_attempts,
            )
        return []

    async def _search_candidates_once(
        self,
        account_id: str,
        skills: List[Any],
        location: str,
        open_to_work: bool,
        limit: int,
        boolean_string: Optional[str],
    ) -> tuple[List[Dict[str, Any]], str]:
        """One search attempt against a specific account.

        Returns (results, outcome) where outcome is one of:
          - "ok":     got a 2xx response (results may be empty — the caller
                      rotates to a sibling when empty).
          - "rotate": a transient failure the caller should retry on another
                      account — an account-level error (already benched here)
                      or a network error/timeout (NOT benched).
          - "fatal":  a bad-payload 4xx that every account would reject
                      identically — the caller should stop, not burn siblings.
          - "classic": 403 errors/feature_not_subscribed — this account has
                      no Recruiter seat; the caller should run
                      `_search_classic_once` on the SAME account (not bench it).
        """
        # 1. Resolve Skill IDs
        skill_ids = []
        # Prioritize Must Have skills
        must_haves = [s for s in skills if (isinstance(s, dict) and s.get("priority") == "Must Have") or (hasattr(s, "priority") and s.priority == "Must Have")]
        other_skills = [s for s in skills if s not in must_haves]

        # Resolve top 5 terms only to keep payload reasonable
        search_terms = (must_haves + other_skills)[:5]

        # LinkedIn Recruiter ANDs every MUST_HAVE skill together, so sending
        # many hard requirements collapses the result set (the root cause of
        # single-result searches when the wizard marked every skill "Must
        # Have"). Cap the hard requirements at UNIPILE_MUST_HAVE_SKILL_CAP;
        # extra must-haves and all preferred terms become CAN_HAVE (OR), which
        # still lifts LinkedIn's relevance ranking without over-constraining.
        try:
            from core import sourcing_config as _sc
            _must_cap = int(getattr(_sc, "UNIPILE_MUST_HAVE_SKILL_CAP", 2) or 2)
        except Exception:
            _must_cap = 2
        _must_cap = max(0, _must_cap)

        must_used = 0
        for s in search_terms:
            name = s.get("value") or s.get("name") if isinstance(s, dict) else getattr(s, "value", getattr(s, "name", str(s)))
            if name:
                 s_id = await self._resolve_id("skill", name, account_id=account_id)
                 if s_id:
                     if s in must_haves and must_used < _must_cap:
                         priority = "MUST_HAVE"
                         must_used += 1
                     else:
                         priority = "CAN_HAVE"
                     skill_ids.append({"id": s_id, "priority": priority, "name_ref": name})
        
        # 2. Resolve Location ID (shared with the classic fallback).
        location_ids = await self._resolve_location_ids(location, account_id)

        # 3. Build Payload using Recruiter API structure
        url = f"{self.api_url}/linkedin/search"

        # Determine keywords
        final_keywords = ""
        if boolean_string:
            final_keywords = self._sanitize_linkedin_keywords(
                boolean_string,
                resolved_skill_names=[s["name_ref"].lower() for s in skill_ids if s.get("name_ref")],
            )
            logger.info(f"Unipile keywords sanitized: '{boolean_string[:120]}...' -> '{final_keywords[:120]}...'")
        else:
            # Prepare keywords for anything we couldn't resolve to an ID
            unresolved_terms = []
            for s in search_terms:
                name = s.get("value") or s.get("name") if isinstance(s, dict) else getattr(s, "value", getattr(s, "name", str(s)))
                # If not in skill_ids (which contains resolved IDs), add to keywords
                if not any(sid.get("name_ref") == name for sid in skill_ids):
                    unresolved_terms.append(f'"{name}"')

            # If location didn't resolve, add to keywords
            if location and not location_ids:
                 loc_term = location.split(",")[0].strip()
                 unresolved_terms.append(f'"{loc_term}"')

            # Keywords fallback for remaining skills
            if len(search_terms) < len(skills):
                extra_skills = skills[len(search_terms):8] # Limit to avoid query too large
                for s in extra_skills:
                    name = s.get("value") or s.get("name") if isinstance(s, dict) else getattr(s, "value", getattr(s, "name", str(s)))
                    if name: unresolved_terms.append(f'"{name}"')

            if unresolved_terms:
                final_keywords = " AND ".join(unresolved_terms)

        # NOTE: we deliberately do NOT append an '("Open to Work" OR
        # "Looking for opportunities")' clause here. LinkedIn Recruiter
        # keywords are a free-text match against profile text, but
        # "Open to Work" is a profile badge/spotlight — almost nobody writes
        # those literal words in their headline/about. ANDing that clause onto
        # every search collapsed the result set to ~1 profile (and a poor one,
        # since the survivor matched the job-seeker phrase, not the role).
        # Open-to-work is now resolved as a real, per-candidate signal via the
        # Apify path (services/apify_open_to_work.py) — see _search_linkedin in
        # unified_candidate_search.py — surfaced as a UI badge and a small
        # score boost. `open_to_work` is retained on this method's signature
        # for backward compatibility but no longer shapes the query.

        payload = {
            "api": "recruiter",
            "category": "people"
        }
        
        logger.info(f"Resolved {len(skill_ids)} skill IDs and {len(location_ids)} location IDs for LinkedIn search")

        if skill_ids:
            payload["skills"] = [{"id": s["id"], "priority": s["priority"]} for s in skill_ids]
            
        if location_ids:
            payload["location"] = [{"id": lid, "priority": "MUST_HAVE"} for lid in location_ids]
        
        if final_keywords:
            payload["keywords"] = final_keywords

        # Page-size ladder: the requested page first; on a Unipile-side 5xx
        # (500 errors/unexpected_error) or upstream timeout (504
        # errors/request_timeout) retry ONCE on the same account with a small
        # page before benching it. A 100-row Recruiter page is four upstream
        # LinkedIn calls, and that is exactly what timed out on PROD
        # (2026-09-09: both seat-holding accounts benched on 500/504 while a
        # 25-row page would have answered).
        try:
            from core import sourcing_config as _sc_retry
            _retry_limit = int(getattr(_sc_retry, "UNIPILE_5XX_RETRY_LIMIT", 25) or 25)
        except Exception:
            _retry_limit = 25
        limits = [limit] + ([_retry_limit] if limit > _retry_limit else [])

        results = []
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                for attempt, lim in enumerate(limits):
                    params = {"account_id": account_id, "limit": lim}
                    logger.info(f"Unipile Recruiter Search Payload (limit={lim}): {json.dumps(payload)}")
                    resp = await client.post(url, params=params, json=payload, headers=self._get_headers())

                    if resp.status_code in [200, 201]:
                        data = resp.json()
                        for item in data.get("items", []):
                            results.append(self._recruiter_item_to_candidate(item, account_id))
                        logger.info(f"Unipile returned {len(results)} candidates from account {account_id}")
                        return results, "ok"

                    body = resp.text
                    logger.error(f"Unipile Search Failed on account {account_id}: {resp.status_code} - {body}")

                    if self._is_no_recruiter_error(resp.status_code, body):
                        # No Recruiter seat on this account. Not a fault —
                        # remember it and let the caller run the classic
                        # people search on the same account.
                        self._recruiter_unavailable[account_id] = time.monotonic()
                        logger.warning(
                            "Unipile: account %s has no LinkedIn Recruiter seat (%s) — using classic search for it",
                            account_id, self._error_type(body) or resp.status_code,
                        )
                        return [], "classic"

                    if resp.status_code >= 500 and attempt + 1 < len(limits):
                        logger.warning(
                            "Unipile: %s (%s) on account %s at limit=%s — retrying once with limit=%s",
                            resp.status_code, self._error_type(body) or "5xx", account_id, lim, limits[attempt + 1],
                        )
                        continue

                    cooldown_s = self.classify_account_failure(resp.status_code, body)
                    if cooldown_s:
                        # Account-level failure: bench it and let the caller
                        # rotate to a sibling.
                        await self.mark_account_failure(account_id, f"search {resp.status_code}: {body[:200]}", cooldown_s)
                        return [], "rotate"
                    # Bad-payload 400: the same request would fail identically
                    # on every account — don't rotate.
                    return [], "fatal"

        except Exception as e:
            # Network-level failure / timeout — not attributable to this
            # account, so don't bench it, but a per-session hang can be
            # account-specific, so let the caller try a sibling.
            logger.error(f"Unipile Search Exception: {e}")
            return [], "rotate"

        return [], "rotate"

    # ------------------------------------------------------------------
    # Shared helpers for the Recruiter and classic search paths
    # ------------------------------------------------------------------
    async def _resolve_location_ids(self, location: str, account_id: str) -> List[str]:
        """LinkedIn geo id(s) for a free-text location. Tries the most
        specific form first: "Dallas, TX" disambiguates against LinkedIn's
        geo index (a bare "Dallas" is ranked by prominence and can hand back
        the wrong same-name town/region), then falls back to the city alone.
        The same ids are valid for both the Recruiter and classic search."""
        location_ids: List[str] = []
        if not (location and location.strip()):
            return location_ids
        loc_parts = [p.strip() for p in location.split(",") if p.strip()]
        attempts = []
        if len(loc_parts) >= 2:
            attempts.append(", ".join(loc_parts[:2]))
        if loc_parts:
            attempts.append(loc_parts[0])
        for loc_term in attempts:
            l_id = await self._resolve_id("location", loc_term, account_id=account_id)
            if l_id:
                location_ids.append(l_id)
                break
        return location_ids

    @staticmethod
    def _error_type(body: Optional[str]) -> str:
        """The `type` of a Unipile error body ("errors/feature_not_subscribed"),
        lowercased, or "" when the body isn't a JSON error envelope."""
        try:
            data = json.loads(body or "")
        except Exception:
            return ""
        if isinstance(data, dict):
            return str(data.get("type") or "").strip().lower()
        return ""

    # Unipile answers a Recruiter-mode search on an account that has no
    # Recruiter seat with 403 + one of these error types.
    _NO_RECRUITER_MARKERS = ("feature_not_subscribed", "feature_not_available", "insufficient_permissions")

    def _is_no_recruiter_error(self, status_code: int, body: Optional[str]) -> bool:
        if status_code != 403:
            return False
        text = self._error_type(body) or (body or "").lower()
        return any(m in text for m in self._NO_RECRUITER_MARKERS)

    def _recruiter_known_unavailable(self, account_id: str) -> bool:
        seen_at = self._recruiter_unavailable.get(account_id)
        if seen_at is None:
            return False
        try:
            from core import sourcing_config as _sc_ttl
            ttl = float(getattr(_sc_ttl, "UNIPILE_NO_RECRUITER_TTL_S", 24 * 3600) or 24 * 3600)
        except Exception:
            ttl = 24 * 3600.0
        if time.monotonic() - seen_at > ttl:
            self._recruiter_unavailable.pop(account_id, None)
            return False
        return True

    def _recruiter_item_to_candidate(self, item: Dict[str, Any], account_id: str) -> Dict[str, Any]:
        """One Recruiter-mode search row -> the shared candidate dict."""
        c_id = item.get("id")
        full_name = self._resolve_candidate_name(item)
        first_name, last_name = self._split_candidate_name(full_name)

        # Handle potential nulls and field variations from docs
        img_url = item.get("img") or item.get("profile_picture_url")
        # Recruiter-mode rows carry TWO links: `profile_url` is a
        # Recruiter deep link (`/talent/search/profile/<hash>`)
        # that only an RPS seat can open, and
        # `public_profile_url` / `public_identifier` is the real
        # vanity URL. Everything downstream — the Step-5 link,
        # Apify open-to-work, ZoomInfo/Apollo/Exa contact
        # enrichment (all keyed on `linkedin.com/in/`), cross-
        # source dedup — needs the PUBLIC one. Preferring the
        # RPS link here paywalled every candidate and silently
        # disabled every URL-keyed enricher for Unipile rows.
        public_url = self._public_profile_url(item)
        recruiter_url = self._recruiter_profile_url(item)
        # Search rows already list endorsed skills; carry them
        # so a row has skill signal even when the profile
        # fetch later fails. Same {"name": ...} shape the
        # profile enrichment produces.
        row_skills = []
        for _s in item.get("skills") or []:
            _name = _s.get("name") if isinstance(_s, dict) else _s
            _name = str(_name or "").strip()
            if _name:
                row_skills.append({"name": _name})
        # Recruiter rows expose the badge as an interest flag.
        # Only a positive signal is trusted: absence is left
        # unset so the Apify resolver still runs for it.
        _interests = item.get("interests") or []
        _open_to_work = isinstance(_interests, list) and any(
            str(_i or "").strip().upper() == "OPEN_TO_WORK" for _i in _interests
        )

        cand = {
            "id": f"unipile_{c_id}",
            "provider_id": c_id,
            "name": full_name,
            "firstName": first_name,
            "lastName": last_name,
            "email": "",
            # LinkedIn's area string ("Toronto, Ontario,
            # Canada" / "Greater Chicago Area"). Exposing it
            # as `location` (not just `city`) lets the parser
            # split city/state/country so the radius and
            # country gates can actually place Unipile rows —
            # with state hardcoded "" they were unplaceable
            # and every location check soft-kept them.
            "location": item.get("location", ""),
            "city": item.get("location", ""),
            "state": "",
            "title": item.get("headline", ""),
            "source": "LinkedIn-Unipile",
            "match_score": 0,
            "profile_url": public_url or "",
            # RPS-only deep link, kept for seat holders; never
            # used as the candidate's identity.
            "recruiter_profile_url": recruiter_url,
            "image_url": img_url,
            # `open_to_work` is intentionally left unset here.
            # It used to be hardcoded to the request-level flag
            # (always True), which (a) mislabeled every profile
            # as open-to-work in the UI and (b) made the frontend
            # poller skip them (it only polls candidates whose
            # open_to_work is not yet a bool). It's now populated
            # from the real Apify signal downstream, exactly like
            # Exa LinkedIn candidates.
            "recruiter_candidate_id": item.get("recruiter_candidate_id"),
            # Account affinity: recruiter_candidate_id and some
            # profile lookups are only valid on the account that
            # performed the search, so downstream enrichment and
            # messaging reuse this account.
            "unipile_account_id": account_id,
            "unipile_search_api": "recruiter",
        }
        if row_skills:
            cand["skills"] = row_skills[:20]
        if _open_to_work:
            cand["open_to_work"] = True
        return cand

    def _classic_item_to_candidate(self, item: Dict[str, Any], account_id: str) -> Dict[str, Any]:
        """One classic-mode search row -> the shared candidate dict.

        Classic rows are public by construction: `id` is the member id
        (`ACoAA…`, which `/users/{id}` and `/chats` accept directly),
        `profile_url` is already the vanity URL, and there is no
        `recruiter_candidate_id`. Skills are not returned in classic mode;
        the profile fetch fills them.
        """
        c_id = item.get("id")
        full_name = self._resolve_candidate_name(item)
        first_name, last_name = self._split_candidate_name(full_name)
        return {
            "id": f"unipile_{c_id}",
            "provider_id": c_id,
            "name": full_name,
            "firstName": first_name,
            "lastName": last_name,
            "email": "",
            "location": item.get("location", ""),
            "city": item.get("location", ""),
            "state": "",
            "title": item.get("headline", ""),
            "source": "LinkedIn-Unipile",
            "match_score": 0,
            "profile_url": self._public_profile_url(item) or "",
            "recruiter_profile_url": None,
            "image_url": item.get("profile_picture_url") or item.get("img"),
            "recruiter_candidate_id": None,
            "unipile_account_id": account_id,
            "unipile_search_api": "classic",
            "linkedin_member_id": c_id,
        }

    async def _search_classic_once(
        self,
        account_id: str,
        skills: List[Any],
        location: str,
        limit: int,
        boolean_string: Optional[str],
    ) -> tuple[List[Dict[str, Any]], str]:
        """LinkedIn *classic* people search on one account.

        The fallback for attached accounts WITHOUT a Recruiter seat — Unipile
        answers their Recruiter-mode search with 403
        errors/feature_not_subscribed (PROD 2026-09-09: two of five
        accounts). Classic search takes the same geo ids and a boolean
        keyword string, returns 10 rows per page, and pages by an opaque
        `cursor` echoed back in the request body. Capped per search at
        UNIPILE_CLASSIC_SEARCH_LIMIT to protect the seat.

        Returns (results, outcome) with the same outcome vocabulary as the
        Recruiter path ("ok" / "rotate" / "fatal").
        """
        try:
            from core import sourcing_config as _sc
            cap = int(getattr(_sc, "UNIPILE_CLASSIC_SEARCH_LIMIT", 50) or 50)
            page_size = int(getattr(_sc, "UNIPILE_CLASSIC_PAGE_SIZE", 10) or 10)
        except Exception:
            cap, page_size = 50, 10
        cap = max(1, min(int(limit or cap), cap))
        page_size = max(1, min(page_size, 10))  # LinkedIn's hard cap for classic pages

        def _name_of(s):
            if isinstance(s, dict):
                return s.get("value") or s.get("name")
            return getattr(s, "value", getattr(s, "name", None)) or (s if isinstance(s, str) else None)

        skill_names = []
        for s in skills or []:
            n = str(_name_of(s) or "").strip()
            if n and n.lower() not in {x.lower() for x in skill_names}:
                skill_names.append(n)

        # Classic has no skill ids — every term rides in the (boolean-capable)
        # keyword string. Reuse the Recruiter sanitizer so JobDiva dialect,
        # radius/country literals and YOE phrases are stripped the same way.
        keywords = ""
        if boolean_string:
            keywords = self._sanitize_linkedin_keywords(boolean_string, resolved_skill_names=[])
        if not keywords and skill_names:
            keywords = " AND ".join(f'"{n}"' for n in skill_names[:4])
        location_ids = await self._resolve_location_ids(location, account_id)
        if not keywords and not location_ids:
            logger.warning("Unipile classic search on %s: nothing to search for (no keywords, no location)", account_id)
            return [], "ok"

        payload: Dict[str, Any] = {"api": "classic", "category": "people"}
        if keywords:
            payload["keywords"] = keywords
        if location_ids:
            payload["location"] = list(location_ids)

        url = f"{self.api_url}/linkedin/search"
        results: List[Dict[str, Any]] = []
        seen: set = set()
        cursor: Optional[str] = None
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                while len(results) < cap:
                    body = dict(payload)
                    if cursor:
                        body["cursor"] = cursor
                    logger.info(f"Unipile Classic Search Payload (limit={page_size}): {json.dumps(body)[:400]}")
                    resp = await client.post(
                        url, params={"account_id": account_id, "limit": page_size}, json=body, headers=self._get_headers()
                    )
                    if resp.status_code not in (200, 201):
                        text = resp.text
                        logger.error(f"Unipile Classic Search Failed on account {account_id}: {resp.status_code} - {text}")
                        if results:
                            break  # keep the pages we already have
                        cooldown_s = self.classify_account_failure(resp.status_code, text)
                        if cooldown_s:
                            await self.mark_account_failure(
                                account_id, f"classic search {resp.status_code}: {text[:200]}", cooldown_s
                            )
                            return [], "rotate"
                        return [], "fatal"
                    data = resp.json()
                    items = data.get("items", []) if isinstance(data, dict) else []
                    if not items:
                        break
                    for item in items:
                        cand = self._classic_item_to_candidate(item, account_id)
                        key = cand.get("provider_id") or cand.get("profile_url")
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        results.append(cand)
                        if len(results) >= cap:
                            break
                    cursor = data.get("cursor") if isinstance(data, dict) else None
                    if not cursor:
                        break
        except Exception as e:
            logger.error(f"Unipile Classic Search Exception on account {account_id}: {e}")
            if not results:
                return [], "rotate"

        logger.info(f"Unipile classic search returned {len(results)} candidates from account {account_id}")
        return results, "ok"

    async def send_message(self, candidate_provider_id: str, text: str, account_id: Optional[str] = None) -> bool:
        """
        Send LinkedIn Message (InMail if premium allowed).
        Pass account_id to send from the account that sourced the candidate.
        """
        account_id = account_id or await self.acquire_account()
        if not account_id: return False
        
        url = f"{self.api_url}/chats"
        
        # Need to handle Multipart/Form or JSON?
        # Docs showed cURL with --form (Multipart).
        # Docs also showed JS client.messaging.startNewChat (JSON).
        # Unipile API usually accepts JSON.
        
        payload = {
            "account_id": account_id,
            "text": text,
            "attendees_ids": [candidate_provider_id],
            "linkedin": {
                "api": "classic",
                "inmail": True
            }
        }
        
        try:
             async with httpx.AsyncClient(timeout=15.0) as client:
                 resp = await client.post(url, json=payload, headers=self._get_headers())
                 if resp.status_code in [200, 201]:
                     return True
                 else:
                     logger.error(f"Unipile Send Message Failed: {resp.text}")
                     return False
        except Exception as e:
            logger.error(f"Unipile Send Message Exception: {e}")
            return False

    async def get_candidate_profile(self, candidate_provider_id: str, account_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fetches full LinkedIn profile for a candidate.
        Endpoint: /linkedin/users/{id} (or generic /users/{id} depending on Unipile version)
        Pass account_id to reuse the account that surfaced the candidate.
        """
        account_id = account_id or await self.acquire_account()
        if not account_id: return None
        
        # Try specific LinkedIn User endpoint
        # verified via debug: /users/{id} works for provider_id
        url = f"{self.api_url}/users/{candidate_provider_id}"
        
        try:
             async with httpx.AsyncClient(timeout=15.0) as client:
                 # `linkedin_sections=*` is REQUIRED. Without it Unipile
                 # returns a ~26-key stub with no experience / education /
                 # skills / summary, so every Unipile candidate was scored on
                 # an empty profile, landed below EXTERNAL_SOURCE_MIN_SCORE and
                 # was dropped — the "LinkedIn returns N profiles, UI shows 0"
                 # failure. With it the payload carries `work_experience`
                 # (role field `position`, dates `start`/`end`), `education`,
                 # `skills`, `certifications` (issuer `organization`),
                 # `summary` and `is_open_to_work`.
                 params = {"account_id": account_id, "linkedin_sections": "*"}
                 resp = await client.get(url, params=params, headers=self._get_headers())
                 
                 if resp.status_code == 200:
                     return resp.json()
                 elif resp.status_code == 404:
                     # Fallback check?
                     logger.warning(f"Unipile Profile 404 for {candidate_provider_id}")
                     return None
                 else:
                     logger.error(f"Unipile Profile Error: {resp.status_code} - {resp.text}")
                     return None
        except Exception as e:
            logger.error(f"Unipile Profile Exception: {e}")
            return None

    def get_account_usage_sync(self) -> List[Dict[str, Any]]:
        """Rotation state for the admin dashboard (DB only, no Unipile call)."""
        if not self._usage_table_ready:
            # Also adds the `search_api` column on older deployments.
            self._ensure_usage_table_sync()
            self._usage_table_ready = True
        from core.db import get_db_connection
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT account_id, account_name, use_count, last_used_at,
                           cooldown_until, last_error, search_api
                    FROM unipile_account_usage
                    ORDER BY use_count DESC, account_id
                """)
                rows = cur.fetchall()
            return [
                {
                    "account_id": r[0],
                    "account_name": r[1] or "",
                    "use_count": int(r[2] or 0),
                    "last_used_at": r[3].isoformat() if r[3] else None,
                    "cooldown_until": r[4].isoformat() if r[4] else None,
                    "last_error": r[5] or "",
                    "search_api": r[6] or None,
                }
                for r in rows
            ]
        finally:
            conn.close()

unipile_service = UnipileService()
