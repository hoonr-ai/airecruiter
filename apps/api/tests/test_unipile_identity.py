"""Unipile LinkedIn identity: candidate names and profile links (2026-08).

LinkedIn Recruiter search rows are keyed by an opaque `AEMAA…` hash, and the
`profile_url` they carry is a Recruiter deep link
(https://www.linkedin.com/talent/search/profile/<hash>) that only an RPS
seat-holder can open. The real vanity URL rides alongside in
`public_profile_url`.

Two failures came out of conflating those:
  * the UI linked every LinkedIn candidate to the Recruiter paywall, and
  * on the ~2% of rows where LinkedIn withholds `name`, the name resolver fell
    back to the recruiter URL's last path segment — and because the hash is
    base64url, its '-'/'_' became spaces, sliding past a "no spaces" ID guard.
    Live examples that reached the UI as candidate names:
        AEMAAGs9ehYBSTb_a4-CPooWbc5YoNZEjTZ0rFo
            -> "Aemaags9ehybstb A4 Cpoowbc5yonzejtz0rfo"  (really: Kani Mozhi.s)
        AEMAAFCG1RkBJwNY5OiJ9nrqEHk0kha-3b40RGE
            -> "Aemaafcg1rkbjwny5oij9nrqehk0kha 3b40rge"  (really: Easwaran U)

Harness pattern matches test_location_sanitizer.py: bare instance via
object.__new__, pure methods exercised directly.
"""
import os

# Config requires these at import time; set dummies before importing anything.
for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY",
):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from services.unipile import UnipileService  # noqa: E402
from services.unified_candidate_search import UnifiedCandidateSearch  # noqa: E402

RECRUITER_HASH = "AEMAAGs9ehYBSTb_a4-CPooWbc5YoNZEjTZ0rFo"
RPS_URL = f"https://www.linkedin.com/talent/search/profile/{RECRUITER_HASH}"


@pytest.fixture
def svc():
    return object.__new__(UnipileService)


@pytest.fixture
def search():
    return object.__new__(UnifiedCandidateSearch)


# --------------------------------------------------------------------------
# opaque-id detection
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    # The two hashes observed reaching the UI as names, post-slug-mangling.
    "Aemaags9ehybstb A4 Cpoowbc5yonzejtz0rfo",
    "Aemaafcg1rkbjwny5oij9nrqehk0kha 3b40rge",
    "Aemaaesrdj8Bputbeeugzft99J0Qcie7Kbhun5K",
    # Raw ids, both LinkedIn families.
    RECRUITER_HASH,
    "ACoAABhH6uQBZ2wDt9Un7UU_xq_Hp5TRcnC4Pus",
    # Digit-free recruiter hash: the '-'/'_' -> space rewrite must not save it.
    "AEMAAGsehYBSTb_ac-CPooWbcYoNZEjTZrFo",
])
def test_opaque_ids_are_not_names(svc, value):
    assert svc._clean_candidate_name(value) is None
    assert svc.is_usable_candidate_name(value) is False


@pytest.mark.parametrize("value", [
    "Bruno Vidal",
    "Kani Mozhi.s",
    "Easwaran U",
    "Dr. Donna Simonton ,DNP,MSN, PNP,RN",
    "Chinenye Onyebuchi,  MA Nursing, BSN, RN, RM, UKRN, USRN, Australian RN",
    # Long, hyphenated and all-caps real names must survive the ID heuristics.
    "Maria del Carmen Rodriguez Fernandez",
    "Anne-Sophie Mutter-Schneider",
    "MARIA DEL CARMEN RODRIGUEZ",
    # Names whose compacted form opens on an id-family prefix. The prefix rule
    # only bites past the length floor, so these must not be indicted:
    # "aceaaronsonbaker", "acqaanantharamanpillai", "aquamarinesanchez".
    "Ace Aaronson Baker",
    "Acqa Anantharaman Pillai",
    "Aqua Marine Sanchez",
])
def test_real_names_survive(svc, value):
    assert svc.is_usable_candidate_name(value) is True


def test_placeholders_rejected(svc):
    for value in ("LinkedIn Candidate", "LinkedIn Member", "unknown", "  "):
        assert svc.is_usable_candidate_name(value) is False


# --------------------------------------------------------------------------
# name derivation reads the PUBLIC url only
# --------------------------------------------------------------------------
def test_recruiter_url_never_yields_a_name(svc):
    assert svc._derive_name_from_profile_url(RPS_URL) is None


def test_public_url_yields_a_name(svc):
    assert svc._derive_name_from_profile_url("https://www.linkedin.com/in/bruno-vidal-") == "Bruno Vidal"


@pytest.mark.parametrize("slug,expected", [
    # LinkedIn appends a numeric disambiguator to most vanity slugs. Stripping
    # it has to happen BEFORE the opaque-id check, or every one of these reads
    # as an identifier and the fallback yields nothing.
    ("kani-mozhi-s-646666422", "Kani Mozhi S"),
    ("rn-deepak-dutta-445484176", "Rn Deepak Dutta"),
    ("bruno-vidal-", "Bruno Vidal"),
    ("akarshagrawal", "Akarshagrawal"),
])
def test_numeric_suffixed_vanity_slugs_still_yield_names(svc, slug, expected):
    assert svc._derive_name_from_slug(slug) == expected


def test_public_url_with_urn_slug_yields_no_name(svc):
    # /in/<member-urn> is a valid LinkedIn URL shape but carries no name.
    assert svc._derive_name_from_profile_url(
        "https://www.linkedin.com/in/ACoAABhH6uQBZ2wDt9Un7UU_xq_Hp5TRcnC4Pus"
    ) is None


def test_resolver_prefers_real_name_over_everything(svc):
    item = {"name": "Bruno Vidal", "profile_url": RPS_URL, "headline": "Backend Engineer"}
    assert svc._resolve_candidate_name(item) == "Bruno Vidal"


def test_resolver_falls_back_to_public_slug_not_recruiter_hash(svc):
    item = {
        "name": None,
        "profile_url": RPS_URL,
        "public_profile_url": "https://www.linkedin.com/in/kani-mozhi-s-646666422",
    }
    assert svc._resolve_candidate_name(item) == "Kani Mozhi S"


def test_resolver_falls_back_to_headline_when_only_a_recruiter_url_exists(svc):
    item = {"name": None, "profile_url": RPS_URL, "headline": "DOH registered nurse"}
    assert svc._resolve_candidate_name(item) == "DOH registered nurse"


def test_resolver_joins_only_present_name_parts(svc):
    # A bare f-string interpolation used to render a missing half as "None".
    assert svc._resolve_candidate_name({"first_name": None, "last_name": "Vidal"}) == "Vidal"


# --------------------------------------------------------------------------
# profile enrichment fills identity the search row lacked
# --------------------------------------------------------------------------
def test_profile_extraction_recovers_identity(search):
    extracted = search._extract_linkedin_profile_data({
        "first_name": "Kani",
        "last_name": "Mozhi.s",
        "public_identifier": "kani-mozhi-s-646666422",
        "provider_id": "ACoAAGs9ehYBTQJdsmGuTNVl09i7a0HO5RpF8j4",
        "location": "Al Ain, Abu Dhabi Emirate, United Arab Emirates",
    })
    assert extracted["profile_name"] == "Kani Mozhi.s"
    # Never keyed as `name`: the caller decides precedence so a good
    # search-row name is not clobbered by a blind .update().
    assert "name" not in extracted
    assert extracted["profile_url"] == "https://www.linkedin.com/in/kani-mozhi-s-646666422"
    assert extracted["linkedin_member_id"] == "ACoAAGs9ehYBTQJdsmGuTNVl09i7a0HO5RpF8j4"


def test_profile_extraction_maps_unipile_section_field_names(search):
    extracted = search._extract_linkedin_profile_data({
        "work_experience": [{
            "company": "VF Corporation",
            "position": "Senior Salesforce Developer",
            "start": "1/1/2022",
            "end": "Present",
            "description": "Built Apex classes and LWC components.",
        }],
        "education": [{"degree": "B.Tech", "school": "JECRC", "start": "1/1/2011", "end": "1/1/2015"}],
        "certifications": [{"name": "Platform Developer II", "organization": "Salesforce"}],
        "skills": [{"name": "Apex", "endorsement_count": 7, "insights": ["noise"], "endorsed": False}],
    })
    exp = extracted["company_experience"][0]
    assert exp["title"] == "Senior Salesforce Developer"   # Unipile calls it `position`
    assert exp["start_date"] == "1/1/2022"
    assert exp["description"] == "Built Apex classes and LWC components."
    # The most recent role's title beats the headline for the role-anchor gate.
    assert extracted["title"] == "Senior Salesforce Developer"
    assert extracted["candidate_education"][0]["year"] == "1/1/2015"
    assert extracted["candidate_certification"][0]["issuer"] == "Salesforce"
    assert extracted["skills"] == [{"name": "Apex", "endorsement_count": 7}]


def test_public_open_to_work_false_never_overrides_the_recruiter_signal(search):
    # is_open_to_work reflects the PUBLIC badge, which reads False for
    # candidates sharing "with recruiters only" — a group the recruiter search
    # row does see. Only a True propagates.
    assert "open_to_work" not in search._extract_linkedin_profile_data({"is_open_to_work": False})
    assert search._extract_linkedin_profile_data({"is_open_to_work": True})["open_to_work"] is True
