"""Unit tests for services/candidate_identity.py — the person key ladder.

Why these tests matter more than usual: this module decides whether two rows are
the same human, and the answer feeds a *suppression* decision. An over-merge
collapses two people into one and silently stops us contacting someone we have
never contacted — a failure with no error message and no obvious symptom. So the
negative cases (name-only, synthetic JobDiva mailbox, placeholder phone) are the
point of this file, not an afterthought.
"""
import os
import sys
from pathlib import Path

for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY", "DATABASE_URL",
):
    os.environ.setdefault(_k, "test")

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from services.candidate_identity import (  # noqa: E402
    KEY_LADDER,
    compute_resume_hash,
    extract_identity_keys,
    normalize_email,
    normalize_phone,
    normalize_profile_url,
    source_cid_key,
)


# ── normalize_profile_url ────────────────────────────────────────────────

def test_url_variants_collapse_to_one_key():
    variants = [
        "https://www.linkedin.com/in/jane-doe-123/",
        "http://linkedin.com/in/jane-doe-123",
        "LinkedIn.com/in/Jane-Doe-123/?utm_source=x",
        "https://www.linkedin.com/in/jane-doe-123#experience",
    ]
    keys = {normalize_profile_url(v) for v in variants}
    assert keys == {"linkedin.com/in/jane-doe-123"}


def test_url_matches_the_shape_exa_service_mints_ids_from():
    # exa_service builds candidate_id as f"exa_{_normalize_linkedin_url(url)}".
    # If these two normalizers disagree, every Exa-sourced candidate fails to
    # resolve to their own person.
    from services.exa_service import _normalize_linkedin_url

    url = "https://www.LinkedIn.com/in/Sam-Smith/?trk=abc"
    assert normalize_profile_url(url) == _normalize_linkedin_url(url)


def test_bare_host_is_not_a_person():
    # A company page or a dead link has no path; keying on it would merge every
    # candidate scraped from that domain into one person.
    assert normalize_profile_url("https://linkedin.com") == ""
    assert normalize_profile_url("https://www.dice.com/") == ""


def test_placeholder_url_rejected():
    assert normalize_profile_url("Available upon request") == ""
    assert normalize_profile_url("") == ""
    assert normalize_profile_url(None) == ""


# ── normalize_email ─────────────────────────────────────────────────────

def test_email_lowercased_and_trimmed():
    assert normalize_email("  Jane.Doe@Example.NET ") == "jane.doe@example.net"


def test_jobdiva_synthetic_email_is_never_an_identity_key():
    # JobDiva mints Auto_<id>@jobdiva.com for candidates with no contact info.
    # Keying on those would merge every such candidate into a single person and
    # suppress outreach to all of them.
    assert normalize_email("Auto_998877@jobdiva.com") == ""
    assert normalize_email("anything@jobdiva.com") == ""
    assert normalize_email("someone@mail.jobdiva.com") == ""


def test_noreply_and_test_domains_rejected():
    assert normalize_email("noreply@acme.com") == ""
    assert normalize_email("real.person@example.com") == ""


def test_placeholder_and_malformed_emails_rejected():
    for bad in ("Available upon request", "n/a", "none", "not provided",
                "jane at example dot com", "jane@", "@example.com", ""):
        assert normalize_email(bad) == "", bad


def test_dots_and_plus_tags_are_preserved():
    # Deliberate: only Gmail treats a.b@ and ab@ as one mailbox. Stripping them
    # globally would merge distinct people at every other provider, and
    # over-merging is the unrecoverable direction.
    assert normalize_email("a.b@outlook.com") == "a.b@outlook.com"
    assert normalize_email("jane+jobs@acme.com") == "jane+jobs@acme.com"


# ── normalize_phone ─────────────────────────────────────────────────────

def test_phone_formats_collapse_to_last_ten_digits():
    for raw in ("+1 (972) 555-0133", "972-555-0133", "19725550133", "972.555.0133"):
        assert normalize_phone(raw) == "9725550133", raw


def test_repeated_digit_placeholders_rejected():
    for bad in ("5555555555", "1111111111", "+1 121-212-1212"):
        assert normalize_phone(bad) == "", bad


def test_too_short_and_invalid_area_codes_rejected():
    assert normalize_phone("555-0133") == ""          # 7 digits
    assert normalize_phone("012-555-0133") == ""      # NANP area codes never start 0
    assert normalize_phone("Available upon request") == ""


# ── resume hash ─────────────────────────────────────────────────────────

def test_resume_hash_is_whitespace_insensitive_and_matches_storage():
    from services.sourced_candidates_storage import _resume_text_hash

    text = "Senior Platform Engineer with ten years building distributed systems "
    text += "across payments and logistics teams."
    spaced = text.replace(" ", "   \n ")
    assert compute_resume_hash(text) == compute_resume_hash(spaced)
    # Must agree with the hash the storage layer already writes into
    # candidate_enhanced_info.resume_hash, or the résumé rung of the ladder never
    # matches anything.
    assert compute_resume_hash(text) == _resume_text_hash(text)


def test_short_text_produces_no_hash():
    # Mirrors the 50-char floor in the storage layer: two people with a one-line
    # "resume" would otherwise hash-collide into one person.
    assert compute_resume_hash("Engineer") == ""


# ── extract_identity_keys ───────────────────────────────────────────────

def test_ladder_order_is_url_then_jobdiva_then_email_then_phone():
    keys = extract_identity_keys({
        "candidate_id": "4455667",
        "source": "JobDiva",
        "profile_url": "https://linkedin.com/in/jane-doe",
        "email": "jane@acme.com",
        "phone": "+1 972-555-0133",
    })
    assert [k[0] for k in keys] == ["linkedin_url", "jobdiva_id", "email", "phone"]
    # Confidence must be non-increasing down the ladder.
    assert [k[2] for k in keys] == sorted([k[2] for k in keys], reverse=True)


def test_name_alone_yields_no_keys():
    # THE over-merge guard. "John Smith" in Dallas is not an identity; a person
    # with nothing else must stay unknown so the launch path gives them a full
    # screen instead of reusing a stranger's screen.
    assert extract_identity_keys({
        "candidate_id": "x1",
        "name": "John Smith",
        "location": "Dallas, TX",
        "source": "LinkedIn-Exa",
    }) == []


def test_synthetic_contacts_yield_no_keys():
    assert extract_identity_keys({
        "candidate_id": "778899",
        "source": "JobDiva-Applicants",
        "name": "Jane Doe",
        "email": "Auto_778899@jobdiva.com",
        "phone": "Available upon request",
        # No profile_url. The only surviving key would be the JobDiva id...
    }) == [("jobdiva_id", "778899", 1.0)]


def test_jobdiva_id_only_trusted_for_jobdiva_sources():
    # A LinkedIn row's candidate_id is an `exa_…` string, not a JobDiva person id.
    # A numeric-looking id from a non-JobDiva source must not be claimed as one.
    keys = extract_identity_keys({
        "candidate_id": "123456",
        "source": "LinkedIn-Exa",
        "email": "sam@acme.com",
    })
    assert ("jobdiva_id", "123456", 1.0) not in keys
    assert [k[0] for k in keys] == ["email"]


def test_jobdiva_id_read_from_data_blob_regardless_of_source():
    keys = extract_identity_keys({
        "candidate_id": "exa_linkedin.com/in/sam",
        "source": "LinkedIn-Exa",
        "data": {"jobdiva_candidate_id": "556677"},
    })
    assert ("jobdiva_id", "556677", 1.0) in keys


def test_keys_read_from_nested_data_blob():
    keys = extract_identity_keys({
        "candidate_id": "abc",
        "source": "LinkedIn-DeepSearch",
        "data": {
            "urls": {"linkedin": "https://www.linkedin.com/in/nested-person/"},
            "email": "nested@acme.com",
        },
    })
    assert ("linkedin_url", "linkedin.com/in/nested-person", 1.0) in keys
    assert ("email", "nested@acme.com", 0.95) in keys


def test_data_blob_accepted_as_json_string():
    # sourced_candidates.data comes back as a dict from psycopg2, but some code
    # paths pass the raw JSON text.
    keys = extract_identity_keys({
        "candidate_id": "abc",
        "source": "Dice",
        "data": '{"email": "str@acme.com"}',
    })
    assert ("email", "str@acme.com", 0.95) in keys


def test_every_ladder_rung_is_a_declared_key_type():
    produced = {
        k[0]
        for k in extract_identity_keys({
            "candidate_id": "4455667",
            "source": "JobDiva",
            "profile_url": "https://linkedin.com/in/x-y",
            "email": "a@b.co",
            "phone": "9725550133",
            "resume_hash": "a" * 64,
        })
    }
    assert produced <= set(KEY_LADDER)
    assert produced == set(KEY_LADDER)


# ── source_cid memo key ─────────────────────────────────────────────────

def test_source_cid_key_is_source_scoped():
    a = source_cid_key({"candidate_id": "999", "source": "JobDiva"})
    b = source_cid_key({"candidate_id": "999", "source": "Dice"})
    assert a != b
    assert a == "jobdiva|999"


def test_source_cid_key_empty_without_candidate_id():
    assert source_cid_key({"source": "JobDiva"}) == ""
