"""UnifiedCandidateSearch methods: dedup."""
from typing import List, Dict, Any
from utils.email_utils import is_placeholder_email


class DedupMixin:

    def _cand_is_jobdiva(self, candidate: Dict[str, Any]) -> bool:
        """True when a candidate carries JobDiva provenance — by `source`
        prefix, by an entry in its merged `sources` list, or by a JobDiva id.
        Used so cross-source dedup never makes a JobDiva row the dropped side.
        """
        if str(candidate.get("source") or "").startswith("JobDiva"):
            return True
        srcs = candidate.get("sources")
        if isinstance(srcs, list) and any(str(s or "").startswith("JobDiva") for s in srcs):
            return True
        return bool(
            str(candidate.get("jobdiva_candidate_id") or candidate.get("jobdiva_id") or "").strip()
        )

    def _merge_candidate_best_of(
        self, dst: Dict[str, Any], src: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fold `src`'s best fields into `dst` (the surviving row) when the two
        are the same person from different sources/ids. `dst` keeps its own
        identity and match score; `src` only fills gaps or upgrades weak values
        (a real email over a synthetic one, a real resume over "unavailable").
        Returns a dict of the fields that changed, suitable for a UI patch.
        """
        changed: Dict[str, Any] = {}
        # Before the sources / JobDiva id below can make it a JobDiva row.
        dst_was_jobdiva = self._cand_is_jobdiva(dst)

        def _src_list(c: Dict[str, Any]) -> List[str]:
            out: List[str] = []
            s = c.get("sources")
            if isinstance(s, list):
                out.extend([str(x) for x in s if x])
            one = c.get("source")
            if one:
                out.append(str(one))
            return out

        merged_sources = list(dict.fromkeys(_src_list(dst) + _src_list(src)))
        cur_sources = dst.get("sources") if isinstance(dst.get("sources"), list) else None
        if merged_sources and merged_sources != cur_sources:
            dst["sources"] = merged_sources
            changed["sources"] = merged_sources

        # Fill missing scalars (incl. JobDiva identity so the survivor stays
        # Launch-PAIR-actionable when it absorbs a JobDiva duplicate).
        for f in (
            "phone", "location", "city", "state", "title", "headline",
            "resume_id", "profile_url", "linkedin_url", "image_url",
            "experience_years", "jobdiva_candidate_id", "jobdiva_id",
        ):
            if not dst.get(f) and src.get(f):
                dst[f] = src[f]
                changed[f] = src[f]

        # Email: prefer a real address over a missing/synthetic one.
        d_email = str(dst.get("email") or "").strip()
        s_email = str(src.get("email") or "").strip()
        if s_email and s_email != d_email and (
            not d_email or (is_placeholder_email(d_email) and not is_placeholder_email(s_email))
        ):
            dst["email"] = s_email
            changed["email"] = s_email
            # Where an adopted email came from ("jobdiva" | "other"), carried
            # through later merges (launch-rules.ts deduplicateCandidatesUI
            # does the same): Launch PAIR's getCandidateSeedEmail never sends
            # one a JobDiva survivor took from a LinkedIn / Exa / Dice row as
            # the email the candidate holds.
            dst["email_source"] = src.get("email_source") or (
                "jobdiva" if self._cand_is_jobdiva(src) else "other"
            )
            changed["email_source"] = dst["email_source"]
        elif d_email and not dst_was_jobdiva and not dst.get("email_source") and self._cand_is_jobdiva(dst):
            # A non-JobDiva survivor the merge just made a JobDiva row (its
            # sources / JobDiva id) keeps its own extracted email: not one
            # JobDiva holds.
            dst["email_source"] = "other"
            changed["email_source"] = "other"

        # Resume text: prefer a real, longer resume over a missing/placeholder one.
        def _bad_resume(r: str) -> bool:
            r = r or ""
            return (not r.strip()) or ("Resume content unavailable" in r)

        d_resume = dst.get("resume_text") or ""
        s_resume = src.get("resume_text") or ""
        if s_resume and not _bad_resume(s_resume) and (_bad_resume(d_resume) or len(s_resume) > len(d_resume)):
            dst["resume_text"] = s_resume
            changed["resume_text"] = s_resume

        # Skills: fill only when the survivor has none (avoid noisy unions).
        d_skills = dst.get("skills") or []
        s_skills = src.get("skills") or []
        if isinstance(s_skills, list) and s_skills and not (isinstance(d_skills, list) and d_skills):
            dst["skills"] = s_skills
            changed["skills"] = s_skills

        return changed

    # Generic company-inbox local parts: a valid, deliverable address that is
    # nonetheless NOT personal identity — contact enrichment can return the
    # same info@/hr@ address for N different profiles at one employer, and an
    # email dedup key would silently collapse all N rows into one.
    _GENERIC_INBOX_LOCALPARTS = frozenset({
        "info", "hr", "careers", "jobs", "contact", "hello", "admin",
        "office", "sales", "support", "recruiting", "recruitment", "team",
        "hiring", "talent",
    })

    def _dedup_keys(self, candidate: Dict[str, Any]) -> List[str]:
        """Cross-source dedup keys for one candidate.

        Only *strong* identity signals are used so two different people never
        collide (a false merge is worse than a duplicate row):
          - `email:` — a real, well-formed, non-synthetic, non-generic-inbox
            address (info@/hr@/… are deliverable but shared, so they are not
            identity).
          - `phone-name:` — a phone (>=7 digits, >=4 distinct, not a shared/
            placeholder line) paired with a full name.
          - `linkedin:` — a normalised LinkedIn URL.
        Name+location alone is intentionally NOT a key — two distinct people
        sharing a common name and city would otherwise be merged.
        """
        keys: List[str] = []

        email = str(candidate.get("email") or "").strip().lower()
        if (
            email and "@" in email
            and not is_placeholder_email(email)
            and email.split("@", 1)[0] not in self._GENERIC_INBOX_LOCALPARTS
        ):
            keys.append(f"email:{email}")

        first = str(candidate.get("firstName") or "").strip().lower()
        last = str(candidate.get("lastName") or "").strip().lower()
        full_name = f"{first} {last}".strip()
        if not full_name:
            full_name = str(candidate.get("name") or "").strip().lower()

        phone_raw = str(candidate.get("phone") or "")
        phone_digits = "".join(filter(str.isdigit, phone_raw))
        # Phone is an identity signal only when paired with a name and not an
        # obviously shared/placeholder line (a single agency number must not
        # collapse a whole agency's candidates into one).
        if (
            len(phone_digits) >= 7
            and len(set(phone_digits)) >= 4
            and full_name
            and " " in full_name
        ):
            keys.append(f"phone-name:{phone_digits[-10:]}|{full_name}")

        profile_url = str(candidate.get("profile_url") or "").strip().lower()
        if profile_url and "linkedin.com" in profile_url:
            normalized = profile_url.split("?", 1)[0].rstrip("/")
            keys.append(f"linkedin:{normalized}")

        return keys

    def _deduplicate_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = {}
        unique_results = []
        
        for cand in candidates:
            email = cand.get("email", "").lower().strip()
            phone_raw = cand.get("phone", "")
            phone_digits = "".join(filter(str.isdigit, phone_raw))
            phone_key = phone_digits[-10:] if len(phone_digits) >= 7 else ""
            
            name = f"{cand.get('firstName', '')} {cand.get('lastName', '')}".lower().strip()
            city = cand.get("city", "").lower().strip()
            
            if email:
                key = f"email:{email}"
            elif phone_key:
                key = f"phone:{phone_key}"
            else:
                key = f"name_loc:{name}|{city}"
            
            if not key or key == "name_loc:|":
                unique_results.append(cand)
                continue
                
            if key not in seen:
                seen[key] = cand
                unique_results.append(cand)
            else:
                existing = seen[key]
                has_both_curr = bool(cand.get("email", "").strip()) and bool(cand.get("phone", "").strip())
                has_both_exist = bool(existing.get("email", "").strip()) and bool(existing.get("phone", "").strip())
                
                # Prioritize: 1. Both email+phone, 2. JobDiva-Applicants
                should_replace = False
                if has_both_curr and not has_both_exist:
                    should_replace = True
                elif not has_both_curr and has_both_exist:
                    should_replace = False
                elif cand.get("source") == "JobDiva-Applicants" and existing.get("source") != "JobDiva-Applicants":
                    should_replace = True

                if should_replace:
                    for i, r in enumerate(unique_results):
                        if r == existing:
                            unique_results[i] = cand
                            break
                    seen[key] = cand
                    
        try:
            from core.newrelic import record_custom_event
            record_custom_event("CandidateSearchSummary", {
                "total_unique_results": len(unique_results),
            })
        except Exception:
            pass

        return unique_results
