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
        if status_code in (401, 403) or any(
            marker in text for marker in ("checkpoint", "disconnected", "credentials", "expired", "invalid account")
        ):
            return _COOLDOWN_AUTH_S
        if status_code == 429 or "rate limit" in text or "too many" in text:
            return _COOLDOWN_RATE_LIMIT_S
        if status_code >= 500:
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

        derived_name = self._derive_name_from_profile_url(
            item.get("profile_url") or item.get("public_profile_url")
        )
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
        has_location_id: bool,
    ) -> str:
        s = boolean_string or ""
        # Drop years-of-experience phrases — LinkedIn profiles rarely contain the exact "10+ years" literal
        s = re.sub(r'"\d+\+\s*years?"', "", s, flags=re.IGNORECASE)
        # Drop JobDiva-dialect experience clauses ("OVER 5 YRS") — the wizard
        # builds one boolean for all ticked sources and keys its dialect on
        # JobDiva, so these tokens leak into LinkedIn searches and match nothing.
        s = re.sub(r'\bOVER\s+\d+\s+YRS?\b', "", s, flags=re.IGNORECASE)
        s = re.sub(r'\s+AND\s+recent', "", s, flags=re.IGNORECASE)
        # Drop location radius clauses when we've already resolved the location to an ID
        if has_location_id:
            s = re.sub(r'"[^"]+"\s+within\s+\d+\s+mi', "", s, flags=re.IGNORECASE)
            s = re.sub(r'within\s+\d+\s+mi', "", s, flags=re.IGNORECASE)
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
        hammering one account. If the claimed account fails with an
        account-level error (checkpoint / expired / rate-limited) it is
        benched and the search retries on up to two sibling accounts.
        """
        tried = set()
        for _ in range(3):
            account_id = await self.acquire_account()
            if not account_id or account_id in tried:
                # No account available, or rotation cycled back to one that
                # already failed — no healthier sibling exists this pass.
                return []
            tried.add(account_id)
            results, account_error = await self._search_candidates_once(
                account_id, skills, location, open_to_work, limit, boolean_string
            )
            if account_error is None:
                return results
        return []

    async def _search_candidates_once(
        self,
        account_id: str,
        skills: List[Any],
        location: str,
        open_to_work: bool,
        limit: int,
        boolean_string: Optional[str],
    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        """One search attempt against a specific account.

        Returns (results, account_error): account_error is non-None only when
        the failure was account-level and the account has been benched, i.e.
        retrying on a different account could succeed.
        """
        # 1. Resolve Skill IDs
        skill_ids = []
        # Prioritize Must Have skills
        must_haves = [s for s in skills if (isinstance(s, dict) and s.get("priority") == "Must Have") or (hasattr(s, "priority") and s.priority == "Must Have")]
        other_skills = [s for s in skills if s not in must_haves]
        
        # Resolve top 5 terms only to keep payload reasonable
        search_terms = (must_haves + other_skills)[:5]
        
        for s in search_terms:
            name = s.get("value") or s.get("name") if isinstance(s, dict) else getattr(s, "value", getattr(s, "name", str(s)))
            if name:
                 s_id = await self._resolve_id("skill", name, account_id=account_id)
                 if s_id:
                     priority = "MUST_HAVE" if s in must_haves else "CAN_HAVE"
                     skill_ids.append({"id": s_id, "priority": priority, "name_ref": name})
        
        # 2. Resolve Location ID
        location_ids = []
        if location and location.strip():
             loc_term = location.split(",")[0].strip()
             l_id = await self._resolve_id("location", loc_term, account_id=account_id)
             if l_id:
                 location_ids.append(l_id)

        # 3. Build Payload using Recruiter API structure
        url = f"{self.api_url}/linkedin/search"
        params = {"account_id": account_id, "limit": limit}
        
        # Determine keywords
        final_keywords = ""
        if boolean_string:
            final_keywords = self._sanitize_linkedin_keywords(
                boolean_string,
                resolved_skill_names=[s["name_ref"].lower() for s in skill_ids if s.get("name_ref")],
                has_location_id=bool(location_ids),
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

        # Handle Open to Work separately or append it
        if open_to_work:
            otw = '("Open to Work" OR "Looking for opportunities")'
            if final_keywords:
                final_keywords = f"({final_keywords}) AND {otw}"
            else:
                final_keywords = otw

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

        results = []
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                logger.info(f"Unipile Recruiter Search Payload: {json.dumps(payload)}")
                resp = await client.post(url, params=params, json=payload, headers=self._get_headers())
                
                if resp.status_code in [200, 201]: 
                    data = resp.json()
                    items = data.get("items", [])
                    
                    for item in items:
                        c_id = item.get("id")
                        full_name = self._resolve_candidate_name(item)
                        first_name, last_name = self._split_candidate_name(full_name)
                        
                        # Handle potential nulls and field variations from docs
                        img_url = item.get("img") or item.get("profile_picture_url")
                        p_url = item.get("profile_url") or item.get("public_profile_url")
                        
                        cand = {
                            "id": f"unipile_{c_id}",
                            "provider_id": c_id,
                            "name": full_name,
                            "firstName": first_name,
                            "lastName": last_name,
                            "email": "",
                            "city": item.get("location", ""),
                            "state": "",
                            "title": item.get("headline", ""),
                            "source": "LinkedIn-Unipile",
                            "match_score": 0,
                            "profile_url": p_url,
                            "image_url": img_url,
                            "open_to_work": open_to_work,
                            "recruiter_candidate_id": item.get("recruiter_candidate_id"),
                            # Account affinity: recruiter_candidate_id and some
                            # profile lookups are only valid on the account that
                            # performed the search, so downstream enrichment and
                            # messaging reuse this account.
                            "unipile_account_id": account_id,
                        }
                        results.append(cand)
                else:
                    body = resp.text
                    logger.error(f"Unipile Search Failed on account {account_id}: {resp.status_code} - {body}")
                    cooldown_s = self.classify_account_failure(resp.status_code, body)
                    if cooldown_s:
                        await self.mark_account_failure(account_id, f"search {resp.status_code}: {body[:200]}", cooldown_s)
                        return [], f"{resp.status_code}"
                    return [], None

        except Exception as e:
            # Network-level failure — not attributable to this account, so
            # don't bench it; a sibling account wouldn't fare better either.
            logger.error(f"Unipile Search Exception: {e}")
            return [], None

        logger.info(f"Unipile returned {len(results)} candidates from account {account_id}")
        return results, None

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
                 params = {"account_id": account_id}
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
        from core.db import get_db_connection
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT account_id, account_name, use_count, last_used_at,
                           cooldown_until, last_error
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
                }
                for r in rows
            ]
        finally:
            conn.close()

unipile_service = UnipileService()
