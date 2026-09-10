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

# LinkedIn's opaque profile identifiers. Recruiter search rows are keyed by an
# `AEMAA…` hash and classic member ids are `ACoAA…`; both are base64url, so
# they carry '-' and '_'. Matched against the separator-stripped, case-folded
# form, and only ever alongside the length floor below — the prefix alone
# would indict a real name like "Ace Aaronson Baker" ("aceaa…").
_LINKEDIN_OPAQUE_ID_RE = re.compile(r"^(?:ac[a-z]aa|aemaa)[a-z0-9]+$")
# Observed ids compact to 34-39 chars. A name that both reaches this length
# AND opens with one of those prefixes is not a realistic collision.
_LINKEDIN_OPAQUE_ID_MIN_LEN = 25


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
        # Last-resort ids for when the /accounts listing is unreachable AND the
        # DB has no rotation history. The legacy single UNIPILE_ACCOUNT_ID is
        # the weakest of these on purpose: it is a stale hand-pinned value in
        # every old deployment and has been observed pointing at an account
        # that was later disconnected, which turned every discovery blip into
        # 100% of traffic hitting a dead seat.
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
                        # Surface workspace-level breakage (e.g. a LinkedIn
                        # account whose session expired -> "CREDENTIALS") on
                        # the admin health view. Without this the account is
                        # merely skipped by rotation and its `last_error`
                        # stays blank, so a disconnected seat looks idle
                        # rather than broken.
                        await self._record_unhealthy_accounts(accounts)
                        return accounts
                    logger.warning("Unipile: no LinkedIn accounts attached to the workspace.")
                else:
                    logger.error(f"Unipile Accounts Error: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Unipile Account Fetch Exception: {e}")

        if self._accounts_cache:
            return self._accounts_cache  # stale beats empty

        # Discovery is down and this worker has never seen the workspace.
        # Prefer accounts the cluster has actually used and that aren't
        # currently benched over the hand-pinned env var.
        try:
            known = await asyncio.to_thread(self._known_good_account_ids_sync)
        except Exception as e:
            logger.warning(f"Unipile: usage-table fallback unavailable ({e}).")
            known = []
        if known:
            logger.warning(
                "Unipile: /accounts unreachable; falling back to %d account(s) from rotation history.",
                len(known),
            )
            return [{"id": a, "name": "", "status": ""} for a in known]
        return [{"id": a, "name": "", "status": ""} for a in self.fallback_account_ids]

    def _known_good_account_ids_sync(self) -> List[str]:
        """Accounts seen by the cluster before and not currently benched."""
        from core.db import get_db_connection
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT account_id FROM unipile_account_usage
                    WHERE cooldown_until IS NULL OR cooldown_until <= NOW()
                    ORDER BY last_used_at DESC NULLS LAST
                """)
                return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    async def _record_unhealthy_accounts(self, accounts: List[Dict[str, Any]]) -> None:
        """Bench accounts the workspace reports as broken, with the reason.

        Cooldown matches the discovery cache TTL, so an account that gets
        reconnected rejoins rotation on the next refresh instead of serving
        out a long bench it no longer deserves.
        """
        unhealthy = [a for a in accounts if a.get("status") and a["status"] != "OK"]
        if not unhealthy:
            return
        for acc in unhealthy:
            logger.warning(
                "Unipile: account %s (%s) is %s in the workspace — excluded from rotation "
                "until it is reconnected in the Unipile dashboard.",
                acc["id"], acc.get("name") or "?", acc["status"],
            )
            try:
                if not self._usage_table_ready:
                    await asyncio.to_thread(self._ensure_usage_table_sync)
                    self._usage_table_ready = True
                await asyncio.to_thread(
                    self._mark_account_failure_sync,
                    acc["id"],
                    f"workspace status: {acc['status']} — reconnect this LinkedIn account in Unipile",
                    _ACCOUNTS_CACHE_TTL_S,
                )
            except Exception as e:
                logger.warning(f"Unipile: could not record unhealthy account {acc['id']}: {e}")

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
        # 404/422 can be account-scoped (e.g. a seat without Recruiter access,
        # or an account-specific resource) — bench briefly and let the caller
        # try a sibling. A genuinely bad payload fails on every sibling and
        # still ends as an empty result, so rotating costs little; treating
        # these as fatal used to zero the whole channel on one bad account.
        if status_code in (404, 422):
            return _COOLDOWN_TRANSIENT_S
        return None

    @staticmethod
    def _looks_like_opaque_id(raw: str) -> bool:
        """True when `raw` is a LinkedIn identifier wearing a name's clothes.

        The separator-insensitive `compact` form is what matters: a recruiter
        hash like `AEMAAGs9ehYBSTb_a4-CPooWbc5YoNZEjTZ0rFo` arrives here as
        "Aemaags9ehybstb A4 Cpoowbc5yonzejtz0rfo" once a URL-slug deriver has
        turned its '-'/'_' into spaces, which is exactly how it used to slip
        past a "no spaces" guard and get stored as a candidate's name.
        """
        compact = re.sub(r"[\s\-_.]+", "", raw)
        if len(compact) < 16 or not compact.isalnum():
            return False
        if (
            len(compact) >= _LINKEDIN_OPAQUE_ID_MIN_LEN
            and _LINKEDIN_OPAQUE_ID_RE.match(compact.casefold())
        ):
            return True
        # Digits surviving inside a long token: real names don't carry them,
        # base64url identifiers almost always do. Callers that work on vanity
        # slugs strip LinkedIn's numeric disambiguator
        # ("kani-mozhi-s-646666422") before asking.
        if any(c.isdigit() for c in compact):
            return True
        # Hash-like case churn. Only indicts a SINGLE unbroken token: a long
        # multi-word name ("Maria Del Carmen Rodriguez Fernandez") carries one
        # capital per word and would otherwise trip this. Meaningful only
        # before a .capitalize() has flattened the case.
        if re.search(r"[\s\-_.]", raw.strip()):
            return False
        return len(re.findall(r"[A-Z]", compact)) > 5 and len(re.findall(r"[a-z]", compact)) > 5

    def _clean_candidate_name(self, value: Optional[str]) -> Optional[str]:
        raw = re.sub(r"\s+", " ", str(value or "")).strip()
        if not raw:
            return None

        normalized = raw.casefold()
        placeholders = {
            "linkedin candidate",
            "linkedin member",
            "professional candidate",
            "unknown candidate",
            "candidate",
            "unknown",
        }
        if normalized in placeholders:
            return None

        if self._looks_like_opaque_id(raw):
            return None

        return raw

    def _derive_name_from_profile_url(self, profile_url: Optional[str]) -> Optional[str]:
        """Best-effort name from a PUBLIC LinkedIn vanity URL (/in/<slug>).

        Deliberately narrow. Recruiter rows also carry a
        `/talent/search/profile/<hash>` URL whose last path segment is an
        opaque id — the old `path_parts[-1]` fallback happily turned that hash
        into a candidate's name.
        """
        if not profile_url:
            return None

        try:
            parsed = urlparse(profile_url)
        except Exception:
            return None

        path_parts = [part for part in (parsed.path or "").split("/") if part]
        if "in" not in path_parts:
            return None
        in_index = path_parts.index("in")
        if in_index + 1 >= len(path_parts):
            return None
        return self._derive_name_from_slug(path_parts[in_index + 1])

    def _derive_name_from_slug(self, slug: Optional[str]) -> Optional[str]:
        """"bruno-vidal-" -> "Bruno Vidal". None when the slug is an id."""
        slug = str(slug or "").strip()
        if not slug:
            return None

        # Normalise separators and drop LinkedIn's numeric disambiguator
        # ("kani-mozhi-s-646666422") BEFORE the id check — otherwise every
        # vanity slug carrying a numeric suffix reads as an identifier.
        slug = re.sub(r"[-_]+", " ", slug).strip()
        slug = re.sub(r"\b\d+\b", " ", slug)
        slug = re.sub(r"\s+", " ", slug).strip()
        if not slug:
            return None

        # Now the id check bites: a recruiter hash keeps its embedded digits
        # and its AEMAA/ACoAA prefix, and is still in its original case.
        if self._looks_like_opaque_id(slug):
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

        # Try first_name + last_name from profile. Join only the parts that
        # survived cleaning — interpolating a None gave names like "None Vidal".
        first_name = self._clean_candidate_name(item.get("first_name") or item.get("firstName"))
        last_name = self._clean_candidate_name(item.get("last_name") or item.get("lastName"))
        if first_name or last_name:
            return " ".join(p for p in (first_name, last_name) if p)

        # PUBLIC vanity URL only — `profile_url` on a recruiter row is the
        # /talent/ deep link, whose slug is an opaque hash.
        derived_name = (
            self._derive_name_from_profile_url(item.get("public_profile_url"))
            or self._derive_name_from_slug(item.get("public_identifier"))
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

    def is_usable_candidate_name(self, value: Optional[str]) -> bool:
        """False for blanks, placeholders and LinkedIn ids masquerading as names.

        Lets callers decide whether a later, better source (e.g. the profile
        fetch) should overwrite a name they already have.
        """
        if self._clean_candidate_name(value) is None:
            return False
        return not str(value or "").strip().startswith("LinkedIn Professional ")

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
            results, outcome = await self._search_candidates_once(
                account_id, skills, location, open_to_work, limit, boolean_string
            )
            if outcome == "fatal":
                # Bad-payload 4xx — a sibling would reject it identically.
                return results
            if outcome == "ok" and results:
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
        
        # 2. Resolve Location ID. Try the most-specific form first:
        # "Dallas, TX" disambiguates against LinkedIn's geo index (which
        # ranks a bare "Dallas" query by prominence and can hand back the
        # wrong same-name town/region), then fall back to the city alone.
        location_ids = []
        if location and location.strip():
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

        # 3. Build Payload using Recruiter API structure
        url = f"{self.api_url}/linkedin/search"
        params = {"account_id": account_id, "limit": limit}
        
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

                        # `profile_url` on a recruiter row is the LinkedIn
                        # Recruiter deep link (/talent/search/profile/<hash>) —
                        # opening it needs an RPS seat, so it is NOT what we
                        # hand the UI. The same row carries the real vanity URL
                        # in `public_profile_url` (or rebuildable from
                        # `public_identifier`). When neither is present the
                        # field stays empty and profile enrichment fills it in
                        # from /users; the recruiter link is preserved
                        # separately for seat-holders and recruiter-scoped ops.
                        public_id = (item.get("public_identifier") or "").strip()
                        recruiter_url = item.get("profile_url") or ""
                        p_url = item.get("public_profile_url") or (
                            f"https://www.linkedin.com/in/{public_id}" if public_id else ""
                        )
                        if not p_url and "/talent/" not in recruiter_url:
                            # A non-recruiter response shape: whatever it gave
                            # us is already a public URL.
                            p_url = recruiter_url

                        # LinkedIn Recruiter tells us open-to-work directly on
                        # the search row — no need to pay Apify to rediscover
                        # it. Absent marker leaves the field unset so the Apify
                        # resolver still runs (a missing `interests` array is
                        # not evidence of "not looking").
                        interests = item.get("interests") or []
                        native_otw = True if any(
                            str(i).upper().endswith("OPEN_TO_WORK") for i in interests
                        ) else None

                        # Endorsed skills ride along on the row; keeping them
                        # gives the scorer real evidence even when the profile
                        # fetch later fails.
                        native_skills = [
                            {"name": s.get("name"), "endorsement_count": s.get("endorsement_count", 0)}
                            for s in (item.get("skills") or [])
                            if isinstance(s, dict) and s.get("name")
                        ]

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
                            "profile_url": p_url,
                            "image_url": img_url,
                            # The RPS-only link, kept out of `profile_url` so
                            # the UI never sends a non-seat-holder to a
                            # Recruiter paywall.
                            "recruiter_profile_url": recruiter_url,
                            "linkedin_public_identifier": public_id,
                            # `open_to_work` is left UNSET unless LinkedIn
                            # itself flagged it (above). It used to be
                            # hardcoded to the request-level flag (always
                            # True), which (a) mislabeled every profile as
                            # open-to-work in the UI and (b) made the frontend
                            # poller skip them (it only polls candidates whose
                            # open_to_work is not yet a bool). Rows without the
                            # marker still fall through to the Apify signal,
                            # exactly like Exa LinkedIn candidates.
                            **({"open_to_work": True} if native_otw else {}),
                            **({"skills": native_skills} if native_skills else {}),
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

        logger.info(f"Unipile returned {len(results)} candidates from account {account_id}")
        return results, "ok"

    async def _resolve_messaging_id(self, candidate_provider_id: str, account_id: str) -> Optional[str]:
        """The classic member id (ACoAA…) usable as a /chats attendee.

        Recruiter search rows are keyed by an AEMAA… hash, which /chats does
        not accept. /users/{hash} resolves it — and also accepts the member id
        itself, so ids that are already correct round-trip unchanged.
        """
        raw = str(candidate_provider_id or "").strip()
        if not raw:
            return None
        if not raw.upper().startswith("AEMAA"):
            return raw
        profile = await self.get_candidate_profile(raw, account_id=account_id)
        member_id = (profile or {}).get("provider_id")
        if member_id:
            return str(member_id)
        return None

    async def send_message(self, candidate_provider_id: str, text: str, account_id: Optional[str] = None) -> bool:
        """
        Send LinkedIn Message (InMail if premium allowed).
        Pass account_id to send from the account that sourced the candidate.
        """
        account_id = account_id or await self.acquire_account()
        if not account_id: return False

        # Candidates sourced through Recruiter search carry the recruiter-scoped
        # hash (AEMAA…) as their provider_id, but /chats attendees must be the
        # classic member id (ACoAA…). Resolve it before sending — passing the
        # recruiter hash silently fails every LinkedIn outreach.
        attendee_id = await self._resolve_messaging_id(candidate_provider_id, account_id)
        if not attendee_id:
            logger.error(
                f"Unipile: cannot message {candidate_provider_id} — no LinkedIn member id resolved."
            )
            return False

        url = f"{self.api_url}/chats"

        # Need to handle Multipart/Form or JSON?
        # Docs showed cURL with --form (Multipart).
        # Docs also showed JS client.messaging.startNewChat (JSON).
        # Unipile API usually accepts JSON.

        payload = {
            "account_id": account_id,
            "text": text,
            "attendees_ids": [attendee_id],
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
        # verified via debug: /users/{id} works for provider_id AND for the
        # recruiter-scoped hash a recruiter search row is keyed by.
        url = f"{self.api_url}/users/{candidate_provider_id}"

        try:
             async with httpx.AsyncClient(timeout=30.0) as client:
                 # Without `linkedin_sections` the response is a 26-key stub:
                 # no experience, education, skills, certifications or summary.
                 # Every Unipile candidate then reached the scorer as a
                 # name/headline/location fragment and ranked off the
                 # parsing-gap floor. '*' returns all sections.
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
