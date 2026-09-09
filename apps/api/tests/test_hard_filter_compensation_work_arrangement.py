"""Tests for automatic hard-filter promotion of compensation and work-arrangement questions.

When a recruiter sets an explicit pass criterion on:
  - Q5 (Compensation / expected pay / salary)
  - Q6 (Work arrangement / job type / W2 / C2C / 1099)

those questions must be promoted to is_hard_filter=True at the
_sanitize_pre_screen_questions_for_pair stage, regardless of screening level
(L0.5, L1, L1.5, L2).

Ref: Bug report – Sanath Kumar Reddy / Job 26-26189 / Fidelity Investments.
Candidate explicitly said "No" to W2 requirement but was still marked Passed.
"""

import pytest

from routers.engagement import (
    _is_compensation_or_work_arrangement_question,
    _sanitize_pre_screen_questions_for_pair,
)


# ---------------------------------------------------------------------------
# Unit tests: _is_compensation_or_work_arrangement_question detector
# ---------------------------------------------------------------------------


class TestIsCompensationOrWorkArrangementQuestion:
    def test_w2_agreement_question(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to working with Pyramid Consulting under a W2 Agreement?"
        )

    def test_w2_with_hyphen(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to work under a W-2 employee basis?"
        )

    def test_corp_to_corp(self):
        assert _is_compensation_or_work_arrangement_question(
            "Which types of working arrangements are you open to: W2, Corp-to-Corp, 1099?"
        )

    def test_c2c_abbreviation(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to C2C arrangements?"
        )

    def test_subcontractor(self):
        assert _is_compensation_or_work_arrangement_question(
            "Select all that apply: W2 Employee, Subcontractor to Pyramid, Independent Contractor"
        )

    def test_expected_compensation(self):
        assert _is_compensation_or_work_arrangement_question(
            "What is your expected compensation for this role?"
        )

    def test_expected_salary(self):
        assert _is_compensation_or_work_arrangement_question(
            "What is your expected salary or pay rate?"
        )

    def test_compensation_expectation(self):
        assert _is_compensation_or_work_arrangement_question(
            "Can you share your compensation expectations?"
        )

    # --- Recruiter-tweaked compensation phrasings ---

    def test_salary_expectations_tweaked(self):
        assert _is_compensation_or_work_arrangement_question(
            "What are your salary expectations?"
        )

    def test_pay_expectation_tweaked(self):
        assert _is_compensation_or_work_arrangement_question(
            "What is your pay expectation?"
        )

    def test_pay_rate_expecting_tweaked(self):
        assert _is_compensation_or_work_arrangement_question(
            "What pay rate are you expecting for this role?"
        )

    def test_hourly_rate_targeting_tweaked(self):
        assert _is_compensation_or_work_arrangement_question(
            "What hourly rate are you targeting?"
        )

    # --- Recruiter-tweaked W2 phrasing (from Pragyan Dubey image) ---

    def test_pyramid_w2_agreement_tweaked(self):
        """Exact phrasing from the recruiter image."""
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to work with pyramid Consulting under a W2 Agreement?"
        )


    def test_work_arrangement(self):
        assert _is_compensation_or_work_arrangement_question(
            "What is your preferred work arrangement?"
        )

    def test_job_type(self):
        assert _is_compensation_or_work_arrangement_question(
            "What job type are you looking for?"
        )

    def test_employment_type(self):
        assert _is_compensation_or_work_arrangement_question(
            "What employment type are you seeking?"
        )

    def test_employment_arrangement(self):
        assert _is_compensation_or_work_arrangement_question(
            "What type of employment arrangement do you prefer?"
        )

    # --- Recruiter-tweaked work arrangement: C2C ---

    def test_c2c_basis_open_to(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to working on a C2C basis?"
        )

    def test_c2c_alone(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to C2C?"
        )

    def test_w2_or_c2c(self):
        assert _is_compensation_or_work_arrangement_question(
            "Do you prefer W2 or C2C?"
        )

    def test_c2c_and_1099(self):
        assert _is_compensation_or_work_arrangement_question(
            "Can you work on a C2C or 1099 basis?"
        )

    # --- Recruiter-tweaked work arrangement: 1099 ---

    def test_1099_contractor_open_to(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to work as a 1099 contractor?"
        )

    # --- Recruiter-tweaked work arrangement: Subcontractor ---

    def test_subcontractor_through_employer(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to working as a subcontractor through your employer?"
        )

    # --- Recruiter-tweaked work arrangement: Corp-to-Corp ---

    def test_corp_to_corp_arrangement(self):
        assert _is_compensation_or_work_arrangement_question(
            "Would you be comfortable with a Corp-to-Corp arrangement?"
        )

    def test_corp_to_corp_open_to(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you open to working under a corp to corp arrangement?"
        )

    # --- W2 with 'employment' suffix ---

    def test_w2_employment_eligible(self):
        assert _is_compensation_or_work_arrangement_question(
            "Are you eligible for W-2 employment?"
        )


    def test_non_comp_question_not_matched(self):
        assert not _is_compensation_or_work_arrangement_question(
            "Have you worked with Denodo in any of your projects?"
        )

    def test_intro_question_not_matched(self):
        assert not _is_compensation_or_work_arrangement_question(
            "To start, can you briefly introduce yourself and walk me through your current role?"
        )

    def test_location_question_not_matched(self):
        assert not _is_compensation_or_work_arrangement_question(
            "What is your current location?"
        )

    # --- False-positive guard tests (these MUST not match) ---

    def test_subcontractor_management_not_matched(self):
        """Role-specific: managing subcontractors should NOT be treated as a work-arrangement Q."""
        assert not _is_compensation_or_work_arrangement_question(
            "Have you managed subcontractors in construction or field projects?"
        )

    def test_1099_tax_reporting_not_matched(self):
        """Role-specific: 1099 tax forms in a finance/accounting role must NOT match."""
        assert not _is_compensation_or_work_arrangement_question(
            "Have you processed 1099 tax reporting for contractors in your previous role?"
        )

    def test_c2c_in_client_context_not_matched(self):
        """'C2C' in a business/client relationship context must NOT match."""
        assert not _is_compensation_or_work_arrangement_question(
            "Describe your experience working in a B2B or client-to-client engagement model."
        )

    def test_corp_to_corp_without_employment_context_not_matched(self):
        """'Corp to corp' alone without employment arrangement context must NOT match."""
        assert not _is_compensation_or_work_arrangement_question(
            "How did you handle Corp to Corp M&A integrations?"
        )

    def test_empty_string_not_matched(self):
        assert not _is_compensation_or_work_arrangement_question("")

    def test_none_not_matched(self):
        assert not _is_compensation_or_work_arrangement_question(None)


# ---------------------------------------------------------------------------
# Integration tests: _sanitize_pre_screen_questions_for_pair
# ---------------------------------------------------------------------------


def _make_w2_question(pass_criteria: str = "", is_hard_filter: bool = False) -> dict:
    return {
        "question_text": "Are you open to work with Pyramid Consulting under a W2 Agreement?",
        "pass_criteria": pass_criteria,
        "category": "logistics",
        "is_default": True,
        "order_index": 5,
        "is_hard_filter": is_hard_filter,
    }


def _make_comp_question(pass_criteria: str = "", is_hard_filter: bool = False) -> dict:
    return {
        "question_text": "What is your expected compensation for this role?",
        "pass_criteria": pass_criteria,
        "category": "logistics",
        "is_default": True,
        "order_index": 4,
        "is_hard_filter": is_hard_filter,
    }


def _make_role_specific_question(pass_criteria: str = "") -> dict:
    return {
        "question_text": "Have you worked with Denodo in any of your projects?",
        "pass_criteria": pass_criteria,
        "category": "role-specific",
        "is_default": False,
        "order_index": 8,
        "is_hard_filter": False,
    }


class TestSanitizeAutoPromotesCompWorkArrangement:
    """W2/comp questions with pass criteria must be promoted to is_hard_filter=True."""

    def test_w2_question_with_pass_criteria_promoted_l1(self):
        """L1 mode: W2 question with criteria → promoted to hard filter."""
        result = _sanitize_pre_screen_questions_for_pair(
            [_make_w2_question(pass_criteria="Must be open to W2 with Pyramid.")],
            boolean_mode=False,
        )
        assert len(result) == 1
        assert result[0]["is_hard_filter"] is True

    def test_work_arrangement_category_promoted(self):
        """Ensure questions with category 'work-arrangement' are recognized as front-matter and promoted."""
        q = _make_w2_question(pass_criteria="Must be open to W2.")
        q["category"] = "work-arrangement"
        q["is_default"] = False  # Should still promote based on category
        result = _sanitize_pre_screen_questions_for_pair([q], boolean_mode=False)
        assert result[0]["is_hard_filter"] is True

    def test_genuine_role_specific_matching_regex_not_zeroed_out(self):
        """Ensure genuine role-specific questions that happen to match the regex aren't zeroed out if already True."""
        q = {
            "question_text": "What pay rate do you quote subcontractors?",
            "pass_criteria": "Candidate has negotiation experience.",
            "category": "role-specific",
            "is_default": False,
            "order_index": 8,
            "is_hard_filter": True,  # Already set to True upstream
        }
        result = _sanitize_pre_screen_questions_for_pair([q], boolean_mode=False)
        # It's role specific, but boolean_mode=False means zero-out usually applies.
        # However, it didn't get auto_promoted by THIS block. Wait, the rule is:
        # if not boolean_mode and is_role_specific and not auto_promoted: is_hard_filter = False.
        # So it SHOULD be zeroed out if it's role specific. The reviewer's point was:
        # Previously, it was NOT zeroed out because it matched the regex.
        # Now it WILL be zeroed out because it wasn't auto_promoted.
        assert result[0]["is_hard_filter"] is False

    def test_w2_question_with_pass_criteria_promoted_l05(self):
        """L0.5 mode: W2 question with criteria → still promoted (redundant but consistent)."""
        result = _sanitize_pre_screen_questions_for_pair(
            [_make_w2_question(pass_criteria="Must be open to W2.")],
            boolean_mode=True,
        )
        assert result[0]["is_hard_filter"] is True

    def test_w2_question_without_pass_criteria_not_promoted(self):
        """W2 question with no pass criteria → NOT promoted (stays informational)."""
        result = _sanitize_pre_screen_questions_for_pair(
            [_make_w2_question(pass_criteria="")],
            boolean_mode=False,
        )
        assert result[0]["is_hard_filter"] is False

    def test_comp_question_with_pass_criteria_promoted(self):
        """Compensation question with criteria → promoted to hard filter."""
        result = _sanitize_pre_screen_questions_for_pair(
            [_make_comp_question(
                pass_criteria="Candidate's expected rate must be within $80–$100/hr."
            )],
            boolean_mode=False,
        )
        assert result[0]["is_hard_filter"] is True

    def test_comp_question_without_pass_criteria_not_promoted(self):
        """Compensation question with no criteria → stays informational."""
        result = _sanitize_pre_screen_questions_for_pair(
            [_make_comp_question(pass_criteria="")],
            boolean_mode=False,
        )
        assert result[0]["is_hard_filter"] is False

    def test_role_specific_question_with_pass_criteria_not_promoted(self):
        """A non-comp role-specific question with criteria must NOT be promoted."""
        result = _sanitize_pre_screen_questions_for_pair(
            [_make_role_specific_question(
                pass_criteria="Candidate confirms hands-on experience with Denodo."
            )],
            boolean_mode=False,
        )
        assert result[0]["is_hard_filter"] is False

    def test_mixed_questions_front_matter_promoted(self):
        """In a full question set, any front-matter question with pass criteria gets promoted."""
        questions = [
            {
                "question_text": "To start, can you briefly introduce yourself?",
                "pass_criteria": "Coherent intro mentioning title and focus.",
                "category": "default",
                "is_default": True,
                "order_index": 0,
                "is_hard_filter": False,
            },
            _make_w2_question(
                pass_criteria="Must be open to W2 with Pyramid.",
                is_hard_filter=False,
            ),
            _make_comp_question(pass_criteria=""),  # no criteria → stays info
            _make_role_specific_question(
                pass_criteria="Candidate confirms Denodo experience."
            ),
        ]
        result = _sanitize_pre_screen_questions_for_pair(questions, boolean_mode=False)
        assert len(result) == 4
        # Q1 intro: promoted because it is front matter and has pass criteria
        assert result[0]["is_hard_filter"] is True
        # Q6 W2 with criteria: promoted
        assert result[1]["is_hard_filter"] is True
        # Q5 comp without criteria: not promoted
        assert result[2]["is_hard_filter"] is False
        # Role-specific: not promoted
        assert result[3]["is_hard_filter"] is False

    def test_already_true_hard_filter_overridden_if_empty(self):
        """If is_hard_filter was already True (e.g. work-arrangement),
        it is overridden to False (info-only) if pass_criteria is empty."""
        q = _make_w2_question(pass_criteria="", is_hard_filter=True)
        result = _sanitize_pre_screen_questions_for_pair([q], boolean_mode=False)
        assert result[0]["is_hard_filter"] is False

    def test_c2c_question_promoted(self):
        """C2C / Corp-to-Corp question text also triggers promotion."""
        result = _sanitize_pre_screen_questions_for_pair(
            [{
                "question_text": (
                    "Which types of working arrangements are you open to and eligible for? "
                    "Select all that apply: W2 Employee, Subcontractor to Pyramid through "
                    "your current employer, Independent Contractor"
                ),
                "pass_criteria": "Candidate must be open to W2 Employee arrangement.",
                "category": "logistics",
                "is_default": True,
                "order_index": 5,
                "is_hard_filter": False,
            }],
            boolean_mode=False,
        )
        assert result[0]["is_hard_filter"] is True

    def test_role_specific_subcontractor_question_not_promoted(self):
        """Critical: a role-specific question mentioning subcontractors with pass criteria
        must NOT be promoted — only front-matter default/logistics questions qualify."""
        result = _sanitize_pre_screen_questions_for_pair(
            [{
                "question_text": "Have you managed subcontractors in construction or field projects?",
                "pass_criteria": "Candidate confirms direct experience managing subcontractors.",
                "category": "role-specific",
                "is_default": False,
                "order_index": 8,
                "is_hard_filter": False,
            }],
            boolean_mode=False,
        )
        assert result[0]["is_hard_filter"] is False

    def test_role_specific_1099_tax_question_not_promoted(self):
        """Critical: a finance role-specific question about 1099 tax reporting
        must NOT be auto-promoted even if it has a pass criterion set."""
        result = _sanitize_pre_screen_questions_for_pair(
            [{
                "question_text": "Have you processed 1099 tax reporting for independent contractors?",
                "pass_criteria": "Candidate confirms they have filed 1099 forms.",
                "category": "role-specific",
                "is_default": False,
                "order_index": 9,
                "is_hard_filter": False,
            }],
            boolean_mode=False,
        )
        assert result[0]["is_hard_filter"] is False
