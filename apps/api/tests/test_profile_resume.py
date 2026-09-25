"""The résumé Launch PAIR gives JobDiva for a person JobDiva does not have yet.

Regression being pinned (2026-09-25): every JobDiva profile PAIR created was
blank. A read-back of PAIR-created profiles showed a 0-byte résumé (3 characters
of text), JobDiva's ``Auto_`` email, and no phone / city / state / experience /
education -- PAIR uploaded no file, and for a LinkedIn row the only text it had
was ``NAME / Email | Phone / (Profile sourced via PAIR)``. These tests pin that
the résumé now carries everything the row holds (services/profile_resume.py).
"""
import io
import zipfile

from services.profile_resume import (
    SECTION_EDUCATION,
    SECTION_EXPERIENCE,
    SECTION_PROFILE_TEXT,
    SECTION_SKILLS,
    SECTION_SUMMARY,
    build_profile_resume,
    clean_profile_text,
    deep_search_profile,
    is_placeholder_name,
    jobdiva_address_fields,
    linkedin_public_identifier,
    normalize_linkedin_profile,
    resume_to_docx,
    split_person_name,
)

# A Unipile /users/{id}?linkedin_sections=* payload (field names as Unipile ships them).
UNIPILE_PROFILE = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "headline": "Principal Data Engineer | Spark | AWS",
    "location": "Jersey City, New Jersey, United States",
    "summary": "Builds data platforms for trading desks.",
    "public_identifier": "ada-lovelace-1",
    "work_experience": [
        {
            "position": "Principal Data Engineer", "company": "Acme Bank", "location": "Jersey City, NJ",
            "start": "1/2021", "end": None, "current": True,
            "description": "Led the lakehouse migration for 40 trading systems.", "skills": ["Spark", "AWS"],
        },
        {"position": "Data Engineer", "company": {"name": "Globex"},
         "start": {"year": 2017, "month": 6}, "end": {"year": 2020, "month": 12}},
    ],
    "education": [{"school": "Stevens Institute of Technology", "degree": "MS",
                   "field_of_study": "Computer Science", "start": "2015", "end": "2017"}],
    "skills": [{"name": "Python", "endorsement_count": 5}, {"name": "SQL"}, "Spark"],
    "certifications": [{"name": "AWS Certified Data Analytics", "organization": "Amazon Web Services"}],
    "languages": [{"name": "English", "proficiency": "Native or bilingual"}],
    "volunteering_experience": [{"role": "Mentor", "company": "Girls Who Code", "start": "2019"}],
    "contact_info": {"emails": ["ada@acme.example"], "phones": [{"number": "+1 201 555 0100"}]},
    "websites": ["https://ada.dev"],
}


def _unipile_row(**over):
    row = {
        "candidate_id": "unipile_AEMAA123", "name": "Ada Lovelace", "headline": "Principal Data Engineer",
        "location": "Jersey City, New Jersey, United States",
        "profile_url": "https://www.linkedin.com/in/ada-lovelace-1", "resume_text": "",
        "source": "LinkedIn-Unipile",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# normalize_linkedin_profile
# ---------------------------------------------------------------------------

def test_normalized_profile_keeps_what_scoring_extraction_drops():
    profile = normalize_linkedin_profile(UNIPILE_PROFILE)

    first_role = profile["experience"][0]
    assert first_role == {
        "title": "Principal Data Engineer", "company": "Acme Bank", "location": "Jersey City, NJ",
        "start": "Jan 2021", "current": True,
        "description": "Led the lakehouse migration for 40 trading systems.", "skills": ["Spark", "AWS"],
    }
    assert profile["experience"][1] == {
        "title": "Data Engineer", "company": "Globex", "start": "Jun 2017", "end": "Dec 2020",
    }
    assert profile["education"] == [{
        "school": "Stevens Institute of Technology", "degree": "MS",
        "field_of_study": "Computer Science", "start": "2015", "end": "2017",
    }]
    assert profile["skills"] == ["Python", "SQL", "Spark"]
    assert profile["certifications"] == [{"name": "AWS Certified Data Analytics", "issuer": "Amazon Web Services"}]
    assert profile["languages"] == [{"name": "English", "proficiency": "Native or bilingual"}]
    assert profile["volunteering"] == [{"role": "Mentor", "organization": "Girls Who Code", "start": "2019"}]
    assert profile["summary"] == "Builds data platforms for trading desks."
    assert profile["public_profile_url"] == "https://www.linkedin.com/in/ada-lovelace-1"
    assert profile["emails"] == ["ada@acme.example"]
    assert profile["phones"] == ["+1 201 555 0100"]


def test_normalization_is_idempotent_so_the_save_path_can_rebound_browser_input():
    once = normalize_linkedin_profile(UNIPILE_PROFILE)
    assert normalize_linkedin_profile(once) == once
    assert normalize_linkedin_profile("not a profile") == {}
    assert normalize_linkedin_profile(None) == {}


def test_normalization_bounds_the_payload():
    huge = {
        "first_name": "A", "last_name": "B",
        "work_experience": [{"position": f"Role {i}", "company": "Co", "description": "x" * 10000} for i in range(50)],
        "skills": [f"skill-{i}" for i in range(500)],
    }
    profile = normalize_linkedin_profile(huge)
    assert len(profile["experience"]) == 20
    assert all(len(role["description"]) <= 3000 for role in profile["experience"])
    assert len(profile["skills"]) == 80


# ---------------------------------------------------------------------------
# build_profile_resume
# ---------------------------------------------------------------------------

def test_unipile_row_resume_carries_the_whole_linkedin_profile():
    resume = build_profile_resume(
        _unipile_row(), {"linkedin_profile": normalize_linkedin_profile(UNIPILE_PROFILE)},
        email="ada@acme.example", phone="+12015550100",
    )
    text = resume.text

    assert text.splitlines()[0] == "Ada Lovelace"
    for expected in (
        "Principal Data Engineer | Spark | AWS",
        "Jersey City, New Jersey, United States",
        "Email: ada@acme.example",
        "Phone: +12015550100",
        "LinkedIn: https://www.linkedin.com/in/ada-lovelace-1",
        SECTION_SUMMARY, "Builds data platforms for trading desks.",
        SECTION_EXPERIENCE, "Principal Data Engineer at Acme Bank", "Jan 2021 - Present · Jersey City, NJ",
        "Led the lakehouse migration for 40 trading systems.",
        "Data Engineer at Globex", "Jun 2017 - Dec 2020",
        SECTION_EDUCATION, "Stevens Institute of Technology", "MS, Computer Science · 2015 - 2017",
        SECTION_SKILLS, "Python, SQL, Spark",
        "AWS Certified Data Analytics · Amazon Web Services",
        "English · Native or bilingual",
        "Mentor at Girls Who Code",
    ):
        assert expected in text, expected
    # The old body -- the whole résumé JobDiva used to get -- is gone.
    assert "(Profile sourced via PAIR)" not in text
    assert resume.is_thin is False and resume.is_blank is False
    assert {"summary", "experience", "education", "skills"} <= set(resume.sections)


def test_row_saved_before_full_profile_capture_still_gets_its_structured_fields():
    """Rows saved before linkedin_profile existed carry the scoring extraction
    (company_experience / education / certifications / skills) -- enough."""
    data = {
        "company_experience": [{"company": "Acme Bank", "title": "Data Engineer", "start_date": "2021", "end_date": "Present"}],
        "education": [{"degree": "BS", "institution": "Rutgers", "year": "2016"}],
        "certifications": [{"name": "CKA", "issuer": "CNCF", "year": "2022"}],
        "skills": [{"name": "Kafka"}, "Scala"],
    }
    resume = build_profile_resume(_unipile_row(), data)

    assert "Data Engineer at Acme Bank" in resume.text
    assert "2021 - Present" in resume.text
    assert "Rutgers" in resume.text and "BS · 2016" in resume.text
    assert "CKA · CNCF · 2022" in resume.text
    assert "Kafka, Scala" in resume.text


def test_exa_row_uses_llm_extraction_plus_the_cleaned_linkedin_crawl():
    row = {
        "name": "Sai Kumar", "headline": "Senior Java Developer", "location": "Jersey City, New Jersey, United States (US)",
        "profile_url": "linkedin.com/in/sai-kumar", "source": "LinkedIn-Exa",
        "resume_text": (
            "Senior Java Developer\n[...]\nJersey City, New Jersey, United States (US)\n[...]\n"
            "### Java Developer at Citi (Current)\nDec 2022 - Present (3 years)\n- Built payment APIs in Spring Boot\n"
            "500 connections • 548 followers"
        ),
    }
    data = {"enhanced_info": {
        "company_experience": [{"company": "Infosys", "title": "Java Developer", "start_date": "Dec 2022",
                                "end_date": "Present", "end_client": "Citi"}],
        "structured_skills": [{"skill": "Java"}, {"skill": "Spring Boot"}],
    }}
    resume = build_profile_resume(row, data)

    assert "Java Developer at Infosys (client: Citi)" in resume.text
    assert "Java, Spring Boot" in resume.text
    assert SECTION_PROFILE_TEXT in resume.text
    assert "Built payment APIs in Spring Boot" in resume.text
    assert "[...]" not in resume.text and "###" not in resume.text
    assert "followers" not in resume.text
    assert resume.text.count("LinkedIn: https://linkedin.com/in/sai-kumar") == 1


def test_deep_search_rationale_never_becomes_the_candidates_resume():
    """LinkedIn-DeepSearch's resume_text is the agent's fit rationale -- PAIR's
    words about the person. It must not be filed in JobDiva as their résumé."""
    entry = {
        "current_title": "Product Manager", "location": "Austin, TX",
        "recent_companies": [{"company": "Initech", "title": "Product Manager", "start": "2021"}],
        "fit_rationale": "Strong fit because they shipped three B2B launches.",
    }
    row = {
        "name": "Neo Zhang", "headline": "Product Manager", "location": "Austin, TX",
        "profile_url": "https://www.linkedin.com/in/neo-zhang", "source": "LinkedIn-DeepSearch",
        "resume_text": "Strong fit because they shipped three B2B launches. Product Manager Initech",
    }
    resume = build_profile_resume(row, {"linkedin_profile": deep_search_profile(entry)})

    assert "Strong fit" not in resume.text
    assert "Product Manager at Initech" in resume.text
    assert resume.sections == ("experience",)


def test_real_resume_is_kept_verbatim_under_a_contact_header():
    resume_body = "JOHN SMITH\nSenior Accountant\n\nEXPERIENCE\nACME Corp — Senior Accountant, 2019-2024\n" * 3
    row = {"name": "John Smith", "headline": "Senior Accountant", "location": "Dallas, TX",
           "source": "Dice", "resume_text": resume_body, "profile_url": ""}
    resume = build_profile_resume(row, {"linkedin_profile": {"summary": "ignored"}}, email="john@smith.example")

    assert resume.sections == ("resume",)
    assert resume.uses_source_resume
    assert resume.text.startswith("John Smith\nSenior Accountant\nDallas, TX\nEmail: john@smith.example\n")
    assert resume_body.strip() in resume.text
    assert "ignored" not in resume.text


def test_placeholder_resume_text_is_not_treated_as_a_resume():
    row = {"name": "Jo Ann", "headline": "Nurse", "source": "Dice", "profile_url": "",
           "resume_text": "Professional experience details available upon request"}
    resume = build_profile_resume(row, {})
    assert "resume" not in resume.sections
    assert "available upon request" not in resume.text


def test_name_only_row_is_blank_and_a_bare_headline_is_thin():
    blank = build_profile_resume({"name": "Ada Lovelace", "source": "LinkedIn-Exa", "resume_text": "resume"}, {})
    assert blank.is_blank and blank.is_thin
    thin = build_profile_resume({"name": "Ada Lovelace", "headline": "Engineer", "source": "LinkedIn-Exa"}, {})
    assert not thin.is_blank and thin.is_thin


def test_placeholder_row_name_falls_back_to_the_linkedin_profile_name():
    resume = build_profile_resume(
        _unipile_row(name="Principal Data Engineer | Spark | AWS"),
        {"linkedin_profile": normalize_linkedin_profile(UNIPILE_PROFILE)},
    )
    assert resume.name == "Ada Lovelace"


def test_work_arrangement_is_never_written_as_the_location():
    resume = build_profile_resume({"name": "Ada Lovelace", "headline": "Engineer", "location": "Remote",
                                   "source": "LinkedIn-Exa"}, {})
    assert resume.location == ""
    assert "Remote" not in resume.text


# ---------------------------------------------------------------------------
# The Word document
# ---------------------------------------------------------------------------

def test_docx_is_a_real_word_document_with_the_resume_in_it():
    resume = build_profile_resume(
        _unipile_row(), {"linkedin_profile": normalize_linkedin_profile(UNIPILE_PROFILE)},
    )
    document = resume_to_docx(resume)

    assert document and document[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    for expected in ("Ada Lovelace", SECTION_EXPERIENCE, "Principal Data Engineer at Acme Bank", "Stevens Institute"):
        assert expected in xml


def test_docx_survives_characters_xml_cannot_carry():
    row = {"name": "Ada Lovelace", "headline": "Eng\x0bineer\x00", "source": "LinkedIn-Exa",
           "resume_text": "Built \x0cthings " * 20}
    assert resume_to_docx(build_profile_resume(row, {}))[:2] == b"PK"


# ---------------------------------------------------------------------------
# Names and addresses for the JobDiva profile fields
# ---------------------------------------------------------------------------

def test_address_fields_for_jobdiva():
    assert jobdiva_address_fields("Jersey City, New Jersey, United States") == {
        "city": "Jersey City", "state": "NJ", "countryid": "US",
    }
    assert jobdiva_address_fields("Jersey City, New Jersey, United States (US)")["state"] == "NJ"
    assert jobdiva_address_fields("Toronto, Ontario, Canada") == {"city": "Toronto", "state": "ON", "countryid": "CA"}
    assert jobdiva_address_fields("Toronto, ON") == {"city": "Toronto", "state": "ON", "countryid": "CA"}
    assert jobdiva_address_fields("Austin, TX 78701") == {"city": "Austin", "state": "TX", "zipCode": "78701", "countryid": "US"}
    assert jobdiva_address_fields("Pune, Maharashtra, India") == {"city": "Pune", "state": "Maharashtra", "countryid": "IN"}
    assert jobdiva_address_fields("Greater Chicago Area") == {"city": "Chicago"}
    assert jobdiva_address_fields("San Francisco Bay Area") == {"city": "San Francisco"}
    assert jobdiva_address_fields("United States") == {"countryid": "US"}
    assert jobdiva_address_fields("CA") == {"state": "CA", "countryid": "US"}  # state slot: California
    assert jobdiva_address_fields("Remote") == {}
    assert jobdiva_address_fields("") == {}


def test_person_name_split():
    assert split_person_name("Ada Lovelace") == ("Ada", "Lovelace")
    assert split_person_name("Dr. Jane van der Berg, PMP, CSM") == ("Jane", "van der Berg")
    assert split_person_name("Jane Doe, Ph.D.") == ("Jane", "Doe")
    assert split_person_name("🚀 Raj Kumar 🚀") == ("Raj", "Kumar")
    assert split_person_name("Madonna") == ("Madonna", "")
    assert split_person_name("") == ("", "")


def test_placeholder_names():
    for name in ("", "Unknown", "Unknown Candidate", "Unnamed Candidate", "LinkedIn Candidate",
                 "LinkedIn Professional ab12cd34", "Data Engineer | Spark", "Data Engineer at Acme",
                 "ada@example.com", "12345"):
        assert is_placeholder_name(name), name
    for name in ("Ada Lovelace", "Li Na", "José Álvarez", "O'Brien Kate", "Ahmad Ali At-Tamimi"):
        assert not is_placeholder_name(name), name


def test_public_identifier_and_profile_text_helpers():
    assert linkedin_public_identifier("https://www.linkedin.com/in/ada-lovelace-1/?trk=x") == "ada-lovelace-1"
    assert linkedin_public_identifier("linkedin.com/in/jos%C3%A9") == "josé"
    assert linkedin_public_identifier("https://www.linkedin.com/talent/profile/AEMAA") == ""
    assert clean_profile_text("## About\n[...]\n**Lead** engineer\n") == "About\n\nLead engineer"
    assert clean_profile_text("Planning &amp; Construction Manager") == "Planning & Construction Manager"
