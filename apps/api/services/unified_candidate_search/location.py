"""UnifiedCandidateSearch methods: location."""
import re
from typing import List, Dict, Any, Optional, Tuple
from services.location import haversine_miles, inferred_region_states, is_broad_region_location, normalize_location_string, sanitize_candidate_location, within_radius
from .common import (
    LocationEntry,
    SearchCriteria,
    _MAX_LOCATIONS,
    _UNKNOWN_DISTANCE_SENTINEL,
    _broad_core_inside_radius,
)


class LocationMixin:

    # 2-letter US state codes for the location heuristic.
    _US_STATE_CODES = frozenset({
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    })

    # Canadian province/territory codes. Disjoint from _US_STATE_CODES, so a
    # 2-letter token identifies its country unambiguously — EXCEPT the codes
    # that collide with _COUNTRY_ALIASES ("CA", "IN", "DE", "NL"): a token in
    # the state slot always resolves as a state/province, never a country.
    _CA_PROVINCE_CODES = frozenset({
        "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE",
        "QC", "SK", "YT",
    })

    # Country aliases → JobDiva expects the 2-letter code in talentSearchDef.
    _COUNTRY_ALIASES = {
        "US": "US", "USA": "US", "U.S.": "US", "U.S.A.": "US",
        "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US",
        "CA": "CA", "CAN": "CA", "CANADA": "CA",
        "UK": "GB", "U.K.": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "GB": "GB",
        "IN": "IN", "INDIA": "IN",
        "AU": "AU", "AUS": "AU", "AUSTRALIA": "AU",
        "DE": "DE", "GERMANY": "DE",
        "FR": "FR", "FRANCE": "FR",
        "MX": "MX", "MEXICO": "MX",
        "BR": "BR", "BRAZIL": "BR",
        "JP": "JP", "JAPAN": "JP",
        "CN": "CN", "CHINA": "CN",
        "SG": "SG", "SINGAPORE": "SG",
        "IE": "IE", "IRELAND": "IE",
        "NL": "NL", "NETHERLANDS": "NL",
        "ES": "ES", "SPAIN": "ES",
        "IT": "IT", "ITALY": "IT",
    }

    # Country values we treat as "US" when present on the candidate record.
    _US_COUNTRY_TOKENS = frozenset({
        "us", "usa", "u.s.", "u.s.a.", "united states",
        "united states of america", "america",
    })

    # Substrings that, when present in a candidate's location text, are
    # confident evidence of a non-US location. Each entry is matched as a
    # word-boundary substring against a space-padded version of the location
    # text, so e.g. " india " matches but "indianapolis" does not.
    _NON_US_LOCATION_TOKENS = frozenset({
        "india", "united kingdom", "canada", "australia", "germany",
        "france", "philippines", "pakistan", "china", "ireland", "mexico",
        "brazil", "spain", "italy", "netherlands", "singapore", "uae",
        "dubai", "saudi arabia", "japan", "south korea", "vietnam",
        "indonesia", "malaysia", "thailand", "egypt", "nigeria",
        "south africa", "russia", "ukraine", "poland", "turkey",
        "israel", "argentina", "chile", "colombia", "peru", "venezuela",
        "bangladesh", "sri lanka", "nepal", "kenya", "ghana", "morocco",
        "switzerland", "sweden", "norway", "denmark", "finland", "belgium",
        "austria", "portugal", "greece", "hungary", "romania",
        "bulgaria", "iran", "iraq", "afghanistan", "qatar", "kuwait",
        "bahrain", "oman", "jordan", "lebanon", "ethiopia", "tanzania",
        "uganda", "zimbabwe", "new zealand", "taiwan", "hong kong",
        "u.k.", "england", "scotland",
    })

    def _resolve_jobdiva_geo(self, criteria: SearchCriteria) -> tuple[List[str], List[str], str]:
        """
        Produce (countries, states, zip_code) for JobDiva's talentSearchDef.

        Priority: explicit `criteria.countries` / `criteria.states` if set;
        otherwise parse `criteria.location`. The Step-5 seed is
        "City, ST ZIP" — the zip must be extracted BEFORE tokenizing
        (it used to ride along as "AZ 85281", fail the len==2 state-code
        check, and silently drop the state from the payload). Always
        defaults to ``["US"]`` when no country can be resolved — searches
        are US-only by policy.
        """
        from services import zip_index

        loc = (criteria.location or "").strip()
        parsed = self._parse_location(loc) if loc else {"city": "", "state": "", "zip": ""}
        zip_code = parsed.get("zip") or ""
        if zip_code and not zip_index.is_known_zip(zip_code):
            zip_code = ""
        # Job has no zip but a resolvable city → use the zip nearest the
        # city centroid so zip+radius search still has something to anchor.
        if not zip_code and parsed.get("city") and parsed.get("state"):
            zip_code = zip_index.city_state_default_zip(parsed["city"], parsed["state"]) or ""

        countries = [c.strip() for c in (criteria.countries or []) if c and c.strip()]
        states = [s.strip() for s in (criteria.states or []) if s and s.strip()]
        if countries or states:
            return countries, states, zip_code

        if not loc:
            # No location criteria at all → still scope to US-only.
            return ["US"], [], ""

        # Country comes from the parser, whose state-slot rule disambiguates
        # "CA": "Los Angeles, CA" is California ⇒ US (the old right-to-left
        # token walk here matched CA-the-country first and scoped every
        # California job to Canada), while "Ajax, ON CA" / "Toronto, ON" ⇒
        # Canada via the trailing token / the ON province code.
        parsed_country = (parsed.get("country") or "").upper()
        if parsed_country:
            countries.append(parsed_country)

        # State: prefer the robust parser (handles "AZ", "Arizona",
        # "AZ 85281", and Canadian provinces like "ON"/"Ontario"), fall back
        # to the raw token walk.
        loc_no_zip = re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", loc)
        tokens = [t.strip() for t in loc_no_zip.split(",") if t.strip()]
        parsed_state = (parsed.get("state") or "").upper()
        if parsed_state and parsed_state in self._US_STATE_CODES:
            states.append(parsed_state)
        elif parsed_state and parsed_state in self._CA_PROVINCE_CODES:
            # JobDiva's talentSearchDef `states` carries provinces for
            # country=CA searches (its own UI labels the field
            # "State/Province").
            states.append(parsed_state)
        else:
            for idx in range(len(tokens) - 1, -1, -1):
                token_upper = tokens[idx].upper()
                if len(token_upper) == 2 and token_upper in self._US_STATE_CODES:
                    states.append(token_upper)
                    break

        # Bare-zip input ("85281") → backfill the state from the zip index.
        if not states and zip_code:
            zip_entry = zip_index.lookup_zip(zip_code)
            if zip_entry and zip_entry["state"].upper() in self._US_STATE_CODES:
                states.append(zip_entry["state"].upper())

        if not countries:
            countries.append("US")

        return countries, states, zip_code

    # Adjacent-state map for the "within N miles spills across state lines"
    # case (Charlotte NC ↔ Fort Mill SC, NYC ↔ Jersey City NJ, etc.). Hand-
    # written, conservative — only the metros recruiters actually ask about.
    # If a state isn't here, _filter_by_state behaves as strict-state.
    _ADJACENT_STATES = {
        "NC": {"SC", "VA", "TN", "GA"},
        "SC": {"NC", "GA"},
        "VA": {"NC", "WV", "MD", "DC", "TN", "KY"},
        "NY": {"NJ", "CT", "PA", "MA", "VT"},
        "NJ": {"NY", "PA", "DE"},
        "PA": {"NY", "NJ", "OH", "WV", "MD", "DE"},
        "CT": {"NY", "MA", "RI"},
        "MA": {"NY", "CT", "RI", "NH", "VT"},
        "MD": {"VA", "PA", "DE", "WV", "DC"},
        "DC": {"MD", "VA"},
        "DE": {"MD", "PA", "NJ"},
        "CA": {"NV", "OR", "AZ"},
        "TX": {"OK", "LA", "AR", "NM"},
        "WA": {"OR", "ID"},
        "OR": {"WA", "CA", "ID", "NV"},
        "IL": {"IN", "WI", "IA", "MO", "KY"},
        "IN": {"IL", "OH", "KY", "MI"},
        "OH": {"PA", "WV", "KY", "IN", "MI"},
        "FL": {"GA", "AL"},
        "GA": {"FL", "AL", "TN", "NC", "SC"},
        "AZ": {"CA", "NV", "UT", "NM"},
        "MI": {"IN", "OH", "WI"},
        "MN": {"WI", "IA", "ND", "SD"},
        "WI": {"IL", "IA", "MN", "MI"},
        "CO": {"WY", "NM", "UT", "KS", "NE", "OK"},
    }

    # Wider regional clusters when within_miles is large (≥200 mi).
    _REGIONAL_CLUSTERS = [
        {"NY", "NJ", "PA", "CT", "MA", "RI"},
        {"NC", "SC", "VA", "TN", "GA"},
        {"TX", "OK", "LA", "AR", "NM"},
        {"CA", "NV", "OR", "AZ", "WA"},
        {"IL", "IN", "OH", "MI", "WI", "MN", "IA", "KY", "MO"},
        {"FL", "GA", "AL", "MS", "LA"},
        {"MD", "DC", "VA", "DE", "PA"},
    ]

    def _filter_by_state(
        self,
        candidates: List[Dict[str, Any]],
        criteria: SearchCriteria,
    ) -> List[Dict[str, Any]]:
        """Location + US-only gate.

        US-only is applied unconditionally; only candidates with positive
        evidence of a non-US location are dropped. The radius/state check
        runs only when a configured location is set. Missing locations are
        deferred while enrichment can still supply one; confirmed radius
        failures are rejected.

        Confirmed out-of-radius and state-mismatch candidates are removed
        before Step 5. Missing locations can continue through enrichment; if
        still unknown, the final emission gate retains them as unverified. A
        location string that could not be geocoded is likewise retained rather
        than misclassified as a confirmed radius failure.
        """
        if not candidates:
            return candidates

        enforce_location = self._should_enforce_location(criteria)
        job_country = self._target_country(criteria)
        from core import sourcing_config as _sc_location
        radius_gate_enabled = getattr(
            _sc_location, "LOCATION_RADIUS_HARD_GATE_ENABLED", True
        )

        kept: List[Dict[str, Any]] = []
        non_us_dropped = 0
        out_of_radius_dropped = 0
        filtered = 0
        geocode_failed = 0

        for c in candidates:
            if self._is_likely_outside_country(c, job_country):
                non_us_dropped += 1
                continue

            if not enforce_location:
                kept.append(c)
                continue

            # The operational rollback must cover the early provider gate as
            # well as the final Step 5 gate. Otherwise turning the switch off
            # cannot recover candidates already discarded here.
            if not radius_gate_enabled:
                kept.append(c)
                continue

            is_match, reason, distance = self._location_match_verdict(c, criteria)
            if distance is not None and distance != _UNKNOWN_DISTANCE_SENTINEL:
                c["distance_miles"] = round(float(distance), 1)
            if reason:
                c["location_match_reason"] = reason
            if is_match:
                kept.append(c)
            elif reason in {"outside_radius_confirmed", "state_mismatch"}:
                # These are confirmed mismatches. Don't let source-specific
                # soft-keep flags bypass the recruiter-configured radius.
                c["location_out_of_radius"] = True
                c["location_veto_reason"] = (
                    "outside_radius_confirmed"
                    if reason == "outside_radius_confirmed"
                    else reason
                )
                if distance is not None and distance != _UNKNOWN_DISTANCE_SENTINEL:
                    c["distance_miles"] = round(float(distance), 1)
                out_of_radius_dropped += 1
            else:
                # Keep unknown locations temporarily so resume/profile
                # enrichment can recover them. The final emission gate is
                # strict and drops them if the radius still can't be checked.
                kept.append(c)
                if reason in {"candidate_ungeocodable", "target_ungeocodable"}:
                    geocode_failed += 1

        self._log_stage(
            "LocationGate",
            f"pre-filter kept {len(kept)}/{len(candidates)} candidates"
            f" (non_us_dropped={non_us_dropped}, out_of_radius_dropped={out_of_radius_dropped},"
            f" filtered={filtered}, geocode_failures={geocode_failed})",
        )
        return kept

    def _location_matches(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> bool:
        is_match, _reason, distance = self._location_match_verdict(candidate, criteria)
        if distance is not None:
            candidate["distance_miles"] = round(float(distance), 1)
        return is_match

    @staticmethod
    def _resume_location_authoritative() -> bool:
        from core import sourcing_config as _sc_loc
        return bool(getattr(_sc_loc, "RESUME_LOCATION_AUTHORITATIVE", True))

    def _apply_resume_location(self, candidate: Dict[str, Any]) -> bool:
        """Post-LLM residence resolution — the résumé is final.

        When the extraction found an explicitly stated current location
        (``enhanced_info.current_location``; the prompt forbids inferring it
        from employers / education), it REPLACES the source-native location
        (JobDiva CRM city/state, LinkedIn area) on ``candidate["location"]``
        for display, scoring and the location / country gates. A JobDiva
        record that says "Dallas, TX" for a résumé headed "Hyderabad, India"
        is rated and launched as India. When the résumé is silent the source
        value stands (legacy behaviour). Both sides go through the
        work-arrangement sanitizer: "Remote"/"Hybrid" is never a place.

        Stamps ``location_source`` ("resume" | "profile"); when the résumé
        overrides a different source value, also ``profile_location`` and
        ``location_conflict = {"resume", "profile"}`` (UI badge + score
        popup line), and clears the cached geo verdict so the gates
        recompute on the final value. Returns True when the résumé won.
        ``RESUME_LOCATION_AUTHORITATIVE=False`` restores fill-blank-only.
        """
        enhanced = candidate.get("enhanced_info")
        enhanced = enhanced if isinstance(enhanced, dict) else {}
        source_loc = sanitize_candidate_location(candidate.get("location"))
        if not source_loc:
            source_loc = sanitize_candidate_location(", ".join(
                p for p in [
                    str(candidate.get("city") or "").strip(),
                    str(candidate.get("state") or "").strip(),
                ] if p
            ))
        resume_loc = sanitize_candidate_location(enhanced.get("current_location"))

        if not resume_loc or not self._resume_location_authoritative():
            candidate["location"] = source_loc or resume_loc
            if candidate["location"]:
                candidate["location_source"] = "profile" if source_loc else "resume"
            return False

        if source_loc and (
            normalize_location_string(source_loc).lower()
            != normalize_location_string(resume_loc).lower()
        ):
            candidate["profile_location"] = source_loc
            candidate["location_conflict"] = {"resume": resume_loc, "profile": source_loc}
        else:
            candidate.pop("location_conflict", None)
        candidate["location"] = resume_loc
        candidate["location_source"] = "resume"
        # The geo verdict cached against the source location is stale now.
        for key in (
            "distance_miles", "location_out_of_radius",
            "location_match_reason", "location_veto_reason",
        ):
            candidate.pop(key, None)
        return True

    def _hydrate_candidate_location_from_zip(self, candidate: Dict[str, Any]) -> bool:
        """Fill a missing display location from a validated US ZIP code."""
        if not isinstance(candidate, dict):
            return False
        existing = sanitize_candidate_location(candidate.get("location"))
        if not existing:
            existing = sanitize_candidate_location(
                ", ".join(
                    part for part in (
                        str(candidate.get("city") or "").strip(),
                        str(candidate.get("state") or "").strip(),
                    ) if part
                )
            )
        if existing:
            candidate["location"] = existing
            return False

        from services import zip_index

        entry = zip_index.lookup_zip(str(candidate.get("zipcode") or "").strip())
        if not entry:
            return False
        city, state = str(entry.get("city") or "").strip(), str(entry.get("state") or "").strip()
        location = sanitize_candidate_location(f"{city}, {state}")
        if not location:
            return False
        candidate["city"] = city
        candidate["state"] = state
        candidate["location"] = location
        candidate["location_source"] = candidate.get("location_source") or "zipcode"
        return True

    def _candidate_structured_locations(self, candidate: Dict[str, Any]) -> List[str]:
        """Return the candidate's current residence locations only.

        Only "where the candidate is now" signals are used — never historical
        job locations, resume free-text locations, or company HQ addresses —
        so the radius filter doesn't accidentally match a candidate to a city
        they worked in five years ago.

        Order of preference (RESUME_LOCATION_AUTHORITATIVE, the default):
        1. ``enhanced_info.current_location`` — the residence the résumé
           states explicitly. When present it is the ONLY value returned:
           the source's city/state must not ride alongside it, or a US CRM
           record would pass the country gate for an India résumé (the exact
           case behind the 2026-09-11 "résumé is final" policy).
        2. ``candidate.city + ", " + candidate.state`` (live source field —
           for JobDiva rows this is the CRM record)
        3. ``candidate.location`` (live source field)
        With the flag off, the legacy order applies: source fields first and
        the résumé value only when they are blank.
        """
        enhanced = candidate.get("enhanced_info") or {}
        enhanced_dict = enhanced if isinstance(enhanced, dict) else {}

        if self._resume_location_authoritative():
            resume_loc = sanitize_candidate_location(enhanced_dict.get("current_location"))
            if resume_loc:
                loc = normalize_location_string(resume_loc)
                return [loc] if loc else []

        city = str(candidate.get("city") or "").strip()
        state = str(candidate.get("state") or "").strip()
        city_state = f"{city}, {state}".strip(", ") if (city or state) else ""

        # Work-arrangement strings ("Remote"/"Hybrid") are stripped before the
        # values feed the geo verdict — they can't geocode, and their presence
        # here used to mask the real residence signal.
        source_native = [
            sanitize_candidate_location(city_state),
            sanitize_candidate_location(candidate.get("location")),
        ]
        location_values = list(source_native)
        if not any(str(v or "").strip() for v in source_native):
            location_values.append(
                sanitize_candidate_location(enhanced_dict.get("current_location"))
            )
        cleaned: List[str] = []
        seen = set()
        for value in location_values:
            loc = normalize_location_string(str(value or ""))
            key = loc.lower()
            if not loc or key in seen:
                continue
            seen.add(key)
            cleaned.append(loc)
        return cleaned

    # Display names for the countries the sourcing pipeline can scope to.
    _COUNTRY_DISPLAY_NAMES = {"US": "United States", "CA": "Canada"}

    def _target_country(self, criteria: SearchCriteria) -> str:
        """The job's country code ("US" default, "CA" for Canadian jobs).

        Resolved from explicit ``criteria.countries`` when the frontend sent
        them, else parsed from ``criteria.location`` ("Toronto, ON" ⇒ CA via
        the province code; "Los Angeles, CA" ⇒ US via the state-slot rule).
        Every non-JobDiva query scope and the outside-country candidate gate
        key off this, so a Canadian job stops searching (and keeping) US-only
        candidates and vice versa.
        """
        for c in (criteria.countries or []):
            code = self._COUNTRY_ALIASES.get(str(c).strip().upper())
            if code:
                return code
        parsed = self._parse_location(criteria.location or "")
        return (parsed.get("country") or "US").upper()

    def _country_display_name(self, country_code: str) -> str:
        return self._COUNTRY_DISPLAY_NAMES.get(
            (country_code or "US").upper(), "United States"
        )

    def _search_location_for_source(self, criteria: SearchCriteria) -> str:
        """Location string to hand external sources (Exa, Dice, Unipile).

        Remote jobs search country-wide — anchoring a remote search to the
        office city just biases discovery toward one metro for no reason.
        Onsite/hybrid keep the job location scoped to the job's country.
        """
        country = self._target_country(criteria)
        if str(getattr(criteria, "location_type", "") or "").strip().lower() == "remote":
            return self._country_display_name(country)
        location = self._resolve_bare_zip_for_external_source(criteria.location)
        return self._scope_location_to_country(location, country)

    @staticmethod
    def _resolve_bare_zip_for_external_source(location: Any) -> Any:
        """A bare ZIP loses all signal once exa_service.py strips its digits
        out before composing the NL query text — resolve it to "City, ST"
        first, same backfill _resolve_jobdiva_geo already does for JobDiva.
        """
        text = str(location or "").strip()
        # Deferred like this file's other zip_index imports (lines 95, 370,
        # 735, 1127) — no circular-import requirement, just this file's
        # convention.
        from services import zip_index
        entry = zip_index.lookup_zip(text)
        if not entry:
            return location
        return f"{entry['city']}, {entry['state']}"

    def _search_location_for_deep_research(self, criteria: SearchCriteria) -> str:
        """Location string for the Exa Pass B agent (a single prompt-based
        call, unlike the fan-out used by the other external sources).

        Pass B is a multi-minute sweep, so it runs once per request rather
        than once per configured location. All configured locations are
        combined into one "or"-joined string so the agent is still aware of
        every additional location instead of only the primary one.
        """
        base = self._search_location_for_source(criteria)
        if str(getattr(criteria, "location_type", "") or "").strip().lower() == "remote":
            return base
        extra = [str(entry.value or "").strip() for entry in (criteria.additional_locations or [])]
        extra = [value for value in extra if value]
        if not extra:
            return base
        return " or ".join([base, *extra]) if base else " or ".join(extra)

    def _location_criteria_variants(self, criteria: SearchCriteria) -> List[SearchCriteria]:
        """Return one independent query criterion per selected location.

        External providers accept one structured location per request.  A
        combined ``New York OR Menlo Park`` string is not a portable geo query,
        so fan out instead and merge/deduplicate the provider results.
        Capped at _MAX_LOCATIONS total variants to protect latency and quota.
        """
        all_entries: List[LocationEntry] = []
        if str(criteria.location or "").strip():
            all_entries.append(LocationEntry(value=criteria.location, within_miles=criteria.within_miles))
        all_entries.extend(criteria.additional_locations)

        # Cap total locations
        all_entries = all_entries[:_MAX_LOCATIONS]

        variants: List[SearchCriteria] = []
        seen = set()
        for entry in all_entries:
            value = str(entry.value or "").strip()
            key = self._normalize_term(value)
            if not key or key in seen:
                continue
            seen.add(key)
            miles = max(1, min(100, int(entry.within_miles)))
            variants.append(criteria.model_copy(update={
                "location": value,
                "within_miles": miles,
                "additional_locations": [],
            }))
        return variants or [criteria]

    def _scope_location_to_country(self, location: Any, country_code: str) -> str:
        """Append the job's country name to a location string when no country
        is already named, so downstream services (Exa, Dice, Vetted, Unipile)
        scope their lookups correctly. A Canadian job used to get ", United
        States" appended here, which made Exa/Unipile search the wrong
        country entirely.

        Returns the bare country name when the input is empty.
        """
        country_name = self._country_display_name(country_code)
        text = str(location or "").strip().strip(",")
        if not text:
            return country_name

        upper_tokens = {t.strip().upper() for t in text.split(",")}
        if upper_tokens & set(self._COUNTRY_ALIASES.keys()):
            return text
        return f"{text}, {country_name}"

    def _scope_location_to_us(self, location: Any) -> str:
        """Back-compat wrapper — US-scoped variant of
        :py:meth:`_scope_location_to_country`."""
        return self._scope_location_to_country(location, "US")

    # Country-field spellings that positively identify Canada.
    _CA_COUNTRY_TOKENS = frozenset({"ca", "can", "canada"})

    def _candidate_country_code(self, candidate: Dict[str, Any]) -> str:
        """Best-effort country code ("US"/"CA"/…) from the candidate record.

        Returns "" when the field is absent or unrecognizable — an
        unparseable value (JobDiva can return ids or free text) is treated
        as "unknown", never as evidence of being abroad.
        """
        enhanced = candidate.get("enhanced_info")
        enhanced_country = (
            enhanced.get("current_country") if isinstance(enhanced, dict) else None
        )
        for raw in (candidate.get("country"), enhanced_country):
            text = str(raw or "").strip().upper()
            if not text:
                continue
            code = self._COUNTRY_ALIASES.get(text)
            if code:
                return code
        return ""

    def _is_likely_outside_country(
        self, candidate: Dict[str, Any], country_code: str = "US"
    ) -> bool:
        """Return True only when the candidate is clearly outside the job's
        country (default US; "CA" for Canadian jobs).

        Defaults to False (treat as in-country) when the candidate's country
        and location text are silent — we want to keep observed candidates
        unless we have positive evidence they're abroad. A Canadian job must
        NOT drop "Toronto, Ontario, Canada" candidates (the old US-only gate
        did exactly that, leaving near-empty external results for CA jobs).
        """
        country_code = (country_code or "US").upper()

        known_code = self._candidate_country_code(candidate)
        if known_code and known_code != country_code:
            return True

        if country_code == "CA":
            foreign_tokens = (self._NON_US_LOCATION_TOKENS - {"canada"}) | {
                "united states", "usa", "u.s.", "u.s.a.",
            }
        else:
            foreign_tokens = self._NON_US_LOCATION_TOKENS

        locs = self._candidate_structured_locations(candidate)
        if locs:
            padded = " " + " | ".join(loc.lower() for loc in locs) + " "
            for token in foreign_tokens:
                # Match as bounded substring to avoid false positives
                # (e.g. "india" must not hit "indianapolis").
                if f" {token} " in padded or f" {token}," in padded:
                    return True
        return False

    def _is_likely_non_us(self, candidate: Dict[str, Any]) -> bool:
        """Back-compat wrapper: outside-US check (US jobs)."""
        return self._is_likely_outside_country(candidate, "US")

    def _location_match_verdict(
        self,
        candidate: Dict[str, Any],
        criteria: SearchCriteria,
    ) -> Tuple[bool, str, Optional[float]]:
        """Multi-location wrapper: try primary location, then each
        additional location. Returns the best (closest) match."""
        additional = criteria.additional_locations
        if not criteria.location and not additional:
            return True, "no_location_requirement", None

        # Try primary location first.
        if criteria.location:
            ok, reason, distance = self._single_location_match_verdict(candidate, criteria)
            # If within radius for primary, return immediately.
            if ok and reason not in ("outside_radius_confirmed", "geocode_unavailable",
                                     "candidate_location_missing"):
                return ok, reason, distance
        else:
            ok, reason, distance = True, "no_primary_location", None

        # Try each additional location — build a temporary criteria with
        # the additional location's value and radius. Return the closest
        # match found across all locations.
        #
        # Precedence (high → low) — order-independent:
        #   1. in-radius (returned immediately above/below)
        #   2. soft-keep  (distance == _UNKNOWN_DISTANCE_SENTINEL)
        #   3. confirmed hard-drop (real distance > radius)
        # Within the same tier, pick the smallest distance.
        # A soft-keep must NEVER be replaced by a hard-drop regardless of
        # which location was evaluated first.
        _SOFT_KEEP_REASONS = frozenset({
            "geocode_unavailable",
            "candidate_location_missing",
            "broad_region_unverified",
            "candidate_state_unknown",
        })
        _HARD_DROP_REASONS = frozenset({"outside_radius_confirmed", "state_mismatch"})

        def _verdict_rank(r: str, d: Optional[float]) -> int:
            """Lower rank = higher priority."""
            if r not in _SOFT_KEEP_REASONS and r not in _HARD_DROP_REASONS:
                return 0  # in-radius (shouldn't be best yet, but be safe)
            if r in _SOFT_KEEP_REASONS or d == _UNKNOWN_DISTANCE_SENTINEL:
                return 1  # soft-keep
            return 2  # hard-drop

        best_ok, best_reason, best_distance = ok, reason, distance
        for loc_entry in additional:
            loc_value = str(loc_entry.value or "").strip()
            if not loc_value:
                continue
            loc_miles = max(1, min(100, int(loc_entry.within_miles)))
            # Build a shallow copy of criteria with this location.
            alt_criteria = criteria.model_copy(update={
                "location": loc_value,
                "within_miles": loc_miles,
                "additional_locations": [],  # prevent recursion
            })
            alt_ok, alt_reason, alt_distance = self._single_location_match_verdict(
                candidate, alt_criteria
            )
            # If within radius for this location, return immediately.
            if alt_ok and alt_reason not in ("outside_radius_confirmed",
                                              "geocode_unavailable",
                                              "candidate_location_missing"):
                return alt_ok, f"multi_loc_{alt_reason}", alt_distance
            # Skip results where the zip-index entirely failed (distance is None):
            # we can't compare them against an existing soft-keep or hard-drop.
            if alt_distance is None:
                continue
            # Promote to best if it is higher-priority, or same tier but closer.
            alt_rank = _verdict_rank(alt_reason, alt_distance)
            best_rank = _verdict_rank(best_reason, best_distance) if best_distance is not None else 3
            if alt_rank < best_rank or (
                alt_rank == best_rank
                and alt_distance != _UNKNOWN_DISTANCE_SENTINEL
                and (best_distance is None or alt_distance < best_distance)
            ):
                best_ok, best_reason, best_distance = alt_ok, alt_reason, alt_distance

        return best_ok, best_reason, best_distance

    def _single_location_match_verdict(
        self,
        candidate: Dict[str, Any],
        criteria: SearchCriteria,
    ) -> Tuple[bool, str, Optional[float]]:
        """Check candidate against a single location from criteria."""
        if not criteria.location:
            return True, "no_location_requirement", None

        # Remote jobs have no commute constraint — any US location passes.
        # (Non-US candidates are still dropped by the _is_likely_non_us gate.)
        if str(getattr(criteria, "location_type", "") or "").strip().lower() == "remote":
            return True, "remote_job_no_location_constraint", None

        from services import zip_index

        required = self._parse_location(criteria.location)

        # Bare-zip input ("85281") or noisy city text: backfill city/state
        # from the offline zip index so every check below still works.
        if required.get("zip"):
            zip_entry = zip_index.lookup_zip(required["zip"])
            if zip_entry:
                if not required["city"]:
                    required["city"] = self._normalize_term(zip_entry["city"])
                if not required["state"]:
                    required["state"] = self._normalize_term(zip_entry["state"])

        if not required["city"] and not required["state"] and not required.get("zip"):
            return True, "empty_location_requirement", None

        candidate_locs = self._candidate_structured_locations(candidate)

        # Direct zip from the source payload (JobDiva JobAgent) — usable even
        # when the candidate's location strings are blank or noisy, and more
        # precise than city-level matching when present.
        direct_zip = ""
        direct_zip_entry = zip_index.lookup_zip(str(candidate.get("zipcode") or "").strip())
        if direct_zip_entry:
            direct_zip = direct_zip_entry["zip"]

        if not candidate_locs and not direct_zip:
            # A candidate without a usable location cannot be confirmed
            # inside any configured radius. It may pass the early source
            # screen so résumé enrichment can recover the location; if it
            # remains unknown, the final gate retains it as unverified.
            return False, "candidate_location_missing", _UNKNOWN_DISTANCE_SENTINEL

        # Metro/region labels (Bay Area, Greater Chicago Area, etc.) are
        # intentionally retained for display but do not identify a precise
        # point. Never use a geocoder's representative centroid to hard-drop
        # these candidates for a small radius. If a separate precise city/ZIP
        # is present, keep evaluating that stronger evidence instead.
        broad_locs = [loc for loc in candidate_locs if is_broad_region_location(loc)]
        precise_locs = []
        precise_candidate_locs = []
        for loc in candidate_locs:
            if is_broad_region_location(loc):
                continue
            parsed_loc = self._parse_location(loc)
            if parsed_loc.get("zip") or (parsed_loc.get("city") and parsed_loc.get("state")):
                precise_locs.append(parsed_loc)
                precise_candidate_locs.append(loc)
        if broad_locs and not direct_zip and not precise_locs:
            known_states = set()
            for loc in broad_locs:
                state = self._parse_location(loc).get("state") or ""
                # Ignore a leftover token such as "united" from
                # "New York, United States". Only a real state code counts,
                # plus the LinkedIn region table (Chicago → IL, Detroit → MI).
                if len(state) == 2 and state.upper() in self._US_STATE_CODES:
                    known_states.add(state.lower())
                for code in inferred_region_states(loc):
                    known_states.add(code.lower())
            if required.get("state") and known_states and required["state"] not in known_states:
                # The state list can still miss a border. Keep the row when
                # the metro's core city sits inside this circle. Chicago
                # against Richardson stays a mismatch: Chicago is outside.
                if _broad_core_inside_radius(broad_locs, required, criteria):
                    return False, "broad_region_unverified", _UNKNOWN_DISTANCE_SENTINEL
                return False, "state_mismatch", None
            if required.get("state") and known_states and not required.get("city"):
                return True, "state_match", None
            return False, "broad_region_unverified", _UNKNOWN_DISTANCE_SENTINEL

        # If search is state-only, enforce state equality without geocoding.
        if required["state"] and not required["city"]:
            seen_states: List[str] = []
            if direct_zip_entry:
                zip_state = self._normalize_term(direct_zip_entry["state"])
                seen_states.append(zip_state)
                if zip_state == required["state"]:
                    return True, "state_match", None
            for value in candidate_locs:
                parsed = self._parse_location(value)
                state = parsed.get("state")
                if state:
                    seen_states.append(state)
                    if state == required["state"]:
                        return True, "state_match", None
            if not seen_states:
                # Defer unparseable text to résumé enrichment. If it remains
                # unknown, the final Step 5 gate retains it as unverified.
                return False, "candidate_state_unknown", _UNKNOWN_DISTANCE_SENTINEL
            return False, "state_mismatch", None

        # Hard cap 100 mi everywhere (defense-in-depth — UI also caps).
        miles = min(100, int(getattr(criteria, "within_miles", 25) or 25))
        geocode_failure = False
        closest_distance: Optional[float] = None

        # OFFLINE FAST-PATH: exact zip / city+state match, then real centroid
        # distances from the offline zip index. JobAgent locations are noisy
        # ("PLANO, TX 75024" vs "Plano, TX" vs "Plano TX") and Nominatim is
        # rate-limited/best-effort, so anything resolvable against the local
        # index gets a deterministic distance with no HTTP call. Nominatim
        # remains only for strings the index can't place (misspellings,
        # neighborhoods, non-standard formats).
        try:
            from services.us_state_index import state_centroid_distance_miles
        except Exception:
            state_centroid_distance_miles = None  # type: ignore[assignment]

        # Resolve the search target to coordinates offline: zip centroid
        # first (most precise), else the city's averaged centroid.
        required_point: Optional[Tuple[float, float]] = None
        if required.get("zip"):
            required_point = zip_index.zip_centroid(required["zip"])
        if required_point is None and required.get("city") and required.get("state"):
            # Representative ZIP, not the average of every ZIP. The average
            # for Richardson sits ~4.5 miles south of the city and was
            # keeping DeSoto, Cedar Hill, and northeast Arlington.
            required_point = zip_index.city_state_representative_point(
                required["city"], required["state"]
            )

        offline_state_mismatch_distance: Optional[float] = None
        best_offline_distance: Optional[float] = None
        unresolved_locs: List[str] = []
        # "Edison" with no state exists in several states. Showing it is
        # right; geocoding it would pin the wrong Edison and hard-drop
        # someone who is inside a circle.
        saw_ambiguous_city = False

        # When an exact city/state signal accompanies a broad region label,
        # evaluate the exact signal and avoid using the region centroid as a
        # second, misleading candidate point.
        parse_targets = list(
            precise_candidate_locs if broad_locs and precise_candidate_locs else candidate_locs
        )
        if direct_zip:
            parse_targets.append(direct_zip)

        for candidate_loc in parse_targets:
            parsed = self._parse_location(candidate_loc)
            cand_city = parsed.get("city", "")
            cand_state = parsed.get("state", "")
            cand_zip = parsed.get("zip", "")

            if cand_city and not cand_state and not cand_zip:
                only_state = zip_index.unique_state_for_city(cand_city)
                if only_state:
                    cand_state = only_state.lower()
                else:
                    saw_ambiguous_city = True
                    continue

            # Exact zip match → same point, ~0 mi.
            if cand_zip and required.get("zip") and cand_zip == required["zip"]:
                return True, "zip_match", 0.0

            # Exact city + state match → same metro, treat as 0 mi.
            if (
                cand_city and cand_state
                and required.get("city") and required.get("state")
                and cand_city == required["city"]
                and cand_state == required["state"]
            ):
                return True, "city_state_match", 0.0

            # Offline centroid distance — candidate zip beats city+state
            # for precision. Deterministic, offline, and immune to geocoder
            # flakiness whenever both sides resolve against the index.
            cand_point = zip_index.zip_centroid(cand_zip) if cand_zip else None
            if cand_point is None and cand_city and cand_state:
                cand_point = zip_index.city_state_representative_point(cand_city, cand_state)
            if cand_point is not None and required_point is not None:
                d = haversine_miles(cand_point[0], cand_point[1], required_point[0], required_point[1])
                if best_offline_distance is None or d < best_offline_distance:
                    best_offline_distance = float(d)
                continue

            # This signal couldn't be placed offline ("Greater Phoenix
            # Area", neighborhoods, misspellings) — it still deserves a
            # Nominatim attempt below. A stale-but-resolvable signal must
            # not confirm the candidate outside while a fresher unresolved
            # one would have geocoded in-radius.
            unresolved_locs.append(candidate_loc)

            # State-centroid distance as a cheap upper-bound estimate when
            # the exact locality can't be resolved — at least the UI can
            # render a real number under BEYOND instead of the sentinel.
            if (
                state_centroid_distance_miles is not None
                and cand_state and required.get("state")
                and cand_state.upper() != required["state"].upper()
            ):
                centroid_d = state_centroid_distance_miles(
                    cand_state.upper(), required["state"].upper()
                )
                if centroid_d is not None:
                    if offline_state_mismatch_distance is None or centroid_d < offline_state_mismatch_distance:
                        offline_state_mismatch_distance = float(centroid_d)

        if best_offline_distance is not None:
            offline_d = round(best_offline_distance, 1)
            if offline_d <= miles:
                return True, "within_radius", offline_d
            # Outside per the offline signals. Only confirmed if EVERY
            # location signal was offline-resolvable; otherwise fall through
            # and let Nominatim try the unresolved strings, seeding the
            # closest-distance with the offline estimate.
            closest_distance = offline_d
            if not unresolved_locs:
                return False, "outside_radius_confirmed", offline_d

        if best_offline_distance is None and saw_ambiguous_city and not unresolved_locs:
            return False, "candidate_state_unknown", _UNKNOWN_DISTANCE_SENTINEL

        # Network path: Nominatim for the strings the offline index couldn't
        # place. Geocode the cleaned "City, ST" reconstruction as target —
        # raw strings with zips/suffixes ("Plano, TX 75024") routinely miss.
        if required.get("city") and required.get("state"):
            target = f"{required['city']}, {required['state']}"
        else:
            target = normalize_location_string(criteria.location)
        for candidate_loc in unresolved_locs:
            is_within, reason, distance = within_radius(candidate_loc, target, miles)
            if isinstance(distance, (int, float)) and distance >= 0:
                if closest_distance is None or distance < closest_distance:
                    closest_distance = float(distance)
            if is_within:
                return True, "within_radius", closest_distance
            if reason in {"candidate_ungeocodable", "target_ungeocodable"}:
                geocode_failure = True

        # Prefer offline cross-state estimate when Nominatim couldn't pin a
        # distance — at least the UI can render a real number under BEYOND.
        if closest_distance is None and offline_state_mismatch_distance is not None:
            closest_distance = offline_state_mismatch_distance

        if geocode_failure:
            # Geocoding failure is not proof of a radius mismatch. Preserve
            # this result as unverified; confirmed distances outside every
            # configured radius are still rejected by the final Step 5 gate.
            return False, "geocode_unavailable", closest_distance if closest_distance is not None else _UNKNOWN_DISTANCE_SENTINEL
        return False, "outside_radius_confirmed", closest_distance if closest_distance is not None else _UNKNOWN_DISTANCE_SENTINEL

    def _should_enforce_location(self, criteria: SearchCriteria) -> bool:
        normalized_location = self._normalize_term(criteria.location)
        if normalized_location:
            return True
        # Multi-location: enforce if any additional location is set.
        for loc in criteria.additional_locations:
            if str(loc.value or "").strip():
                return True
        return False

    def _parse_location(self, value: Any) -> Dict[str, str]:
        state_aliases = {
            "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
            "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
            "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
            "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
            "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
            "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
            "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
            "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
            "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
            "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
            "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
            "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
            "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
            # Canadian provinces/territories — full names map to their
            # 2-letter codes (disjoint from every US state code), so
            # "Toronto, Ontario" and "Toronto, ON" compare equal.
            "alberta": "ab", "british columbia": "bc", "manitoba": "mb",
            "new brunswick": "nb", "newfoundland and labrador": "nl",
            "newfoundland": "nl", "nova scotia": "ns",
            "northwest territories": "nt", "nunavut": "nu", "ontario": "on",
            "prince edward island": "pe", "quebec": "qc", "québec": "qc",
            "saskatchewan": "sk", "yukon": "yt",
        }
        from services.us_state_index import resolve_state_code

        text = self._normalize_search_text(value)
        text = re.sub(r"\bwithin\s+\d+\s+mi\b", "", text)
        text = re.sub(r"\bmetro\b", "", text)
        text = re.sub(r"^must be local to\s+", "", text).strip(" ,")

        # Extract a 5-digit US zip (with optional ZIP+4 suffix) before
        # splitting on comma. This is a normalization fix for inputs like
        # "Plano, TX 75024" / "PLANO TX 75024-1234" / "75024" — zip stays
        # accessible for downstream offline match instead of being stripped.
        # Take the LAST match: in address-style strings ("10001 W Main St,
        # Mesa, AZ 85201") the leading 5-digit number is a street number,
        # and the zip — when present — trails.
        zip_matches = list(re.finditer(r"\b(\d{5})(?:-\d{4})?\b", text))
        zip_match = zip_matches[-1] if zip_matches else None
        zip_code = zip_match.group(1) if zip_match else ""
        if zip_code:
            text = (text[:zip_match.start()] + text[zip_match.end():]).strip(" ,")

        parts = [part.strip() for part in re.split(r",|\\|/", text) if part.strip()]

        city = parts[0] if parts else ""
        state = ""
        # Words of parts[1] that are NOT the state name. "New Jersey" is two
        # words; taking only "New" left Edison and Poughkeepsie unresolvable
        # and soft-kept them as if the profile had no location.
        state_part_leftover: List[str] = []
        if len(parts) > 1:
            state_tokens = parts[1].split()
            resolved_state = ""
            consumed = 0
            for n in (3, 2, 1):
                if len(state_tokens) < n:
                    continue
                code = resolve_state_code(" ".join(state_tokens[:n]))
                if code:
                    resolved_state = code
                    consumed = n
                    break
            if resolved_state:
                state = resolved_state
                state_part_leftover = state_tokens[consumed:]
            elif state_tokens:
                state = state_tokens[0]
                state_part_leftover = state_tokens[1:]
        elif len(parts) == 1:
            tokens = parts[0].split()
            # Bare state input ("CA", "California", "Calif") — recognise via
            # the local us_state_index so we hit the state-only fast path in
            # _location_match_verdict instead of soft-keeping on a Nominatim
            # miss. Bug F: production used to silently accept NC candidates
            # for a "CA" job because geocoding "CA" was ambiguous.
            bare_state = resolve_state_code(parts[0])
            if bare_state:
                city = ""
                state = bare_state
            elif len(tokens) > 1 and len(tokens[-1]) == 2:
                city = " ".join(tokens[:-1])
                state = tokens[-1]
            elif len(tokens) > 1:
                # "City State" (no comma, full or abbreviated). Try resolving
                # the trailing token(s) as a state name.
                for n in (2, 1):
                    if len(tokens) > n:
                        candidate_state = resolve_state_code(" ".join(tokens[-n:]))
                        if candidate_state:
                            city = " ".join(tokens[:-n])
                            state = candidate_state
                            break

        normalized_state = state_aliases.get(self._normalize_term(state), self._normalize_term(state))

        # ---- country detection -------------------------------------------
        # Rule: a token in the STATE SLOT (right after the city) is always a
        # state/province, never a country — "Los Angeles, CA" is California,
        # not Canada. Countries are read from the tokens AFTER the state
        # ("Ajax, ON CA", "Toronto, Ontario, Canada"), or inferred from the
        # state code itself (ON ⇒ Canada, TX ⇒ US). A state-slot token that
        # is ONLY a country name ("Toronto, Canada") moves to country.
        country = ""
        after_state_tokens: List[str] = []
        after_state_tokens.extend(state_part_leftover)
        for extra_part in parts[2:]:
            after_state_tokens.extend(extra_part.split())
        for tok in reversed([t.strip() for t in after_state_tokens if t.strip()]):
            tok_upper = tok.upper()
            if tok_upper in self._COUNTRY_ALIASES:
                country = self._COUNTRY_ALIASES[tok_upper]
                break
        # Multi-word country names split across tokens ("United States").
        if not country and len(parts) > 1:
            tail_text = " ".join(after_state_tokens).upper().strip(" .")
            if tail_text in self._COUNTRY_ALIASES:
                country = self._COUNTRY_ALIASES[tail_text]
        state_upper = normalized_state.upper()
        if not country and state_upper:
            if state_upper in self._CA_PROVINCE_CODES:
                country = "CA"
            elif state_upper in self._US_STATE_CODES:
                country = "US"
            elif state_upper in self._COUNTRY_ALIASES:
                # "Toronto, Canada" — the state slot held a country name.
                country = self._COUNTRY_ALIASES[state_upper]
                normalized_state = ""

        # Cross-validate the zip against the state: a street number in an
        # address-style string ("10001 W Main St, Mesa, AZ") can collide
        # with a real zip on the other side of the country. When any part
        # of the string resolves to a US state and the zip's own state
        # disagrees, the 5-digit number wasn't a zip — drop it. Unknown
        # zips (not in the index) are kept for the exact-equality match.
        if zip_code:
            from services import zip_index
            zip_entry = zip_index.lookup_zip(zip_code)
            if zip_entry:
                state_hint = resolve_state_code(normalized_state) if normalized_state else None
                if not state_hint:
                    for part in reversed(parts[1:]):
                        first_token = part.split()[0] if part.split() else ""
                        state_hint = resolve_state_code(first_token)
                        if state_hint:
                            break
                if state_hint and zip_entry["state"].upper() != state_hint.upper():
                    zip_code = ""
            # Foreign postal codes collide with US zips ("Paris, 75001,
            # France" — 75001 is Addison, TX). A named non-US country in
            # the string disqualifies the number as a US anchor.
            if zip_code and any(
                p.strip().lower() in self._NON_US_LOCATION_TOKENS for p in parts
            ):
                zip_code = ""

        return {
            "city": self._normalize_term(city),
            "state": normalized_state,
            "zip": zip_code,
            "country": country,
        }

    def _location_hard_gate(self, candidate: Dict[str, Any], criteria: SearchCriteria) -> Optional[str]:
        """Hard-drop only candidates *confirmed* outside the radius; soft-keep everything else.

        The two hard-drop cases are:
          - ``state_mismatch``: candidate's state is known and differs from the
            required state.
          - ``outside_radius_confirmed``: geocoding succeeded and the measured
            distance exceeds every configured radius.

        Candidates whose location is blank, broad (e.g. "San Francisco Bay Area"),
        or temporarily unresolvable (geocoder outage) are soft-kept so that a
        data gap is never mistaken for a confirmed out-of-radius result. The
        recruiter sees an "unverified location" badge for these rows.

        The strict gate can be disabled operationally with
        LOCATION_RADIUS_HARD_GATE_ENABLED.
        """
        for key in (
            "location_veto_reason", "location_out_of_radius",
            "distance_miles", "location_match_reason",
        ):
            candidate.pop(key, None)
        if not self._should_enforce_location(criteria):
            return None
        ok, reason, distance = self._location_match_verdict(candidate, criteria)
        if distance is not None:
            candidate["distance_miles"] = (
                None if distance == _UNKNOWN_DISTANCE_SENTINEL else round(float(distance), 1)
            )
        # Stamp the distance badge for all known out-of-radius candidates.
        if (
            isinstance(distance, (int, float))
            and distance != _UNKNOWN_DISTANCE_SENTINEL
            and reason == "outside_radius_confirmed"
        ):
            candidate["location_out_of_radius"] = True
            candidate.setdefault("location_match_reason", reason)

        if ok:
            return None

        candidate["location_match_reason"] = reason
        if reason == "geocode_unavailable":
            # We have a location string but the geocoder could not resolve
            # it. Don't turn a transient network failure into a false
            # confirmed mismatch; retain the row without an out-of-radius
            # badge and let the recruiter see the known text location.
            return None

        from core import sourcing_config as _sc_location
        if not getattr(_sc_location, "LOCATION_RADIUS_HARD_GATE_ENABLED", True):
            # Kill switch preserves the radius badge/reason while allowing
            # results through for rollback during a geodata incident.
            return None

        if reason == "state_mismatch":
            # Machine-readable veto marker for the emit-time drop gates and
            # telemetry (the human string below feeds explainability only).
            candidate["location_veto_reason"] = reason
            return f"location outside {criteria.location}"
        if (
            reason == "outside_radius_confirmed"
            and isinstance(distance, (int, float))
            and distance != _UNKNOWN_DISTANCE_SENTINEL
        ):
            candidate["location_veto_reason"] = "outside_radius_confirmed"
            return f"location {round(float(distance))}mi outside every configured radius"

        # Unresolvable locations (blank profile, broad region like "San Francisco
        # Bay Area", or any other case where we cannot confirm the candidate is
        # outside the radius) are soft-kept. Hard-drop is reserved exclusively
        # for candidates whose distance is *confirmed* to exceed the limit
        # (outside_radius_confirmed above) or who are in the wrong state
        # (state_mismatch above). Dropping on mere inability to geocode would
        # incorrectly remove valid local candidates whose location strings are
        # imprecise (e.g. "Greater Chicago Area") or temporarily unavailable.
        # The recruiter sees an "unverified location" badge instead.
        candidate["location_match_reason"] = reason or "location_unverified"
        if distance == _UNKNOWN_DISTANCE_SENTINEL:
            candidate["distance_miles"] = None
        # Do NOT return a veto string — soft-keep the candidate.
        return None
