"""services/launch_person_keys.py — who counts as "the same person" for the
launch gate, and the drop decision the claims run on top of it."""

from services import launch_person_keys as lpk


def test_person_keys_cover_email_phone_linkedin_and_jobdiva_id():
    row = {
        "candidate_id": "exa_x", "source": "LinkedIn-Exa",
        "email": "  Jane.Doe@Acme.io ", "phone": "+1 (415) 555-0142",
        "profile_url": "https://www.linkedin.com/in/Jane-Doe-42/?trk=x",
        "data": {"jobdiva_candidate_id": "4471"},
    }
    assert lpk.person_keys(row) == [
        "email:jane.doe@acme.io", "phone:4155550142", "linkedin:jane-doe-42", "jobdiva:4471",
    ]


def test_a_jobdiva_rows_own_id_is_a_key_but_a_linkedin_rows_numeric_id_is_not():
    assert lpk.person_keys({"candidate_id": "4471", "source": "JobDiva-Applicant"}) == ["jobdiva:4471"]
    assert lpk.person_keys({"candidate_id": "4471", "source": "LinkedIn"}) == []


def test_placeholders_never_make_two_people_one():
    for email in ("Auto_123@jobdiva.com", "pair-4155550142@no-email.jobdiva.local", "x@noemail.pair.ai",
                  "Available upon request", "test@example.com", ""):
        assert lpk.email_key(email) == "", email
    for phone in ("000-000-0000", "555-555-5555", "1111111111", "555-0142", ""):
        assert lpk.phone_key(phone) == "", phone
    # Recruiter-seat /talent/ links are opaque per seat, never a person key.
    assert lpk.linkedin_key("https://www.linkedin.com/talent/profile/AEMAAABC") == ""


def test_linkedin_urls_normalise_to_one_slug():
    for url in ("http://linkedin.com/in/jane-doe", "https://in.linkedin.com/in/Jane-Doe/",
                "https://www.linkedin.com/in/jane-doe?originalSubdomain=uk", "linkedin.com/pub/jane-doe"):
        assert lpk.linkedin_key(url) == "jane-doe", url
    assert lpk.linkedin_key("https://www.linkedin.com/in/j%C3%B6rg") == "jörg"


def test_data_fields_are_read_when_the_columns_are_empty():
    row = {"candidate_id": "c", "data": {"email": "a@b.co", "phone": "4155550142",
                                         "urls": {"linkedin": "https://linkedin.com/in/ab"}}}
    assert lpk.person_keys(row) == ["email:a@b.co", "phone:4155550142", "linkedin:ab"]


def test_holds_person():
    assert lpk.holds_person({"engage_interview_id": "iv"})
    assert lpk.holds_person({"data": {"engage_interview_id": "iv", "engage_status": "failed"}})
    for status in ("queued", "sending", "processing", "sent"):
        assert lpk.holds_person({"engage_status": status}), status
    for status in ("", "failed", "pending"):
        assert not lpk.holds_person({"engage_status": status}), status
    # The claiming session's own not-yet-posted siblings never block each other …
    assert not lpk.holds_person({"engage_status": "queued", "engage_session_id": "S1"}, session_id="S1")
    # … but its posted ones do, and another session's queue does.
    assert lpk.holds_person({"engage_status": "sending", "engage_session_id": "S1"}, session_id="S1")
    assert lpk.holds_person({"engage_status": "queued", "engage_session_id": "S2"}, session_id="S1")


def test_find_person_duplicates_drops_ids_of_a_held_person():
    holders = [("exa_jane", ["email:jane@x.com", "phone:4155550142"])]
    requested = [("4471", ["email:jane@x.com"]), ("bob", ["email:bob@x.com"]), ("exa_jane", ["email:jane@x.com"])]
    assert lpk.find_person_duplicates(requested, holders) == {"4471": ("exa_jane", "email")}


def test_find_person_duplicates_keeps_the_first_copy_within_one_request():
    requested = [("a", ["phone:4155550142", "email:a@x.com"]), ("b", ["email:b@x.com", "phone:4155550142"]),
                 ("c", ["email:c@x.com"]), ("a", ["linkedin:a"])]
    assert lpk.find_person_duplicates(requested, []) == {"b": ("a", "phone")}


def test_a_dropped_copys_other_contacts_belong_to_the_same_person():
    # b is a's person (phone), so b's email is that person's too: c, sharing
    # only that email, is the same human again.
    requested = [("a", ["phone:4155550142"]), ("b", ["phone:4155550142", "email:p@x.com"]),
                 ("c", ["email:p@x.com"])]
    assert lpk.find_person_duplicates(requested, []) == {"b": ("a", "phone"), "c": ("a", "email")}
    holders = [("h", ["linkedin:jane"])]
    requested = [("x", ["linkedin:jane", "email:jane@x.com"]), ("y", ["email:jane@x.com"])]
    assert lpk.find_person_duplicates(requested, holders) == {"x": ("h", "linkedin"), "y": ("h", "email")}


def test_launched_person_keys_only_from_holding_rows():
    rows = [
        {"candidate_id": "a", "email": "a@x.com", "engage_interview_id": "iv"},
        {"candidate_id": "b", "email": "b@x.com", "engage_status": "failed"},
        {"candidate_id": "c", "phone": "4155550142", "data": {"engage_status": "processing"}},
    ]
    assert lpk.launched_person_keys(rows) == {"email:a@x.com": "a", "phone:4155550142": "c"}


# ---------------------------------------------------------------------------
# Golden cases shared with apps/web/lib/launch-person-keys.ts
# ---------------------------------------------------------------------------
import json  # noqa: E402
from pathlib import Path  # noqa: E402

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "launch_person_keys.json").read_text())


def test_golden_person_keys_match_the_browser():
    for case in _FIXTURE["keys"]:
        assert lpk.person_keys(case["row"]) == case["keys"], case["row"]


def test_golden_launched_person_blocks_match_the_browser():
    for case in _FIXTURE["blocks"]:
        assert lpk.launched_person_blocks(case["pool"], case["owners"]) == case["blocked"]
