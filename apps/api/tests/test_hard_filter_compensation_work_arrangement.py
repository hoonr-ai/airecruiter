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

    def test_mixed_questions_only_comp_promoted(self):
        """In a full question set, only the comp/work-arrangement gets promoted."""
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
        # Q1 intro: not promoted
        assert result[0]["is_hard_filter"] is False
        # Q6 W2 with criteria: promoted
        assert result[1]["is_hard_filter"] is True
        # Q5 comp without criteria: not promoted
        assert result[2]["is_hard_filter"] is False
        # Role-specific: not promoted
        assert result[3]["is_hard_filter"] is False

    def test_already_true_hard_filter_preserved(self):
        """If is_hard_filter was already True (e.g. work-arrangement on onsite role),
        it stays True even when pass_criteria is empty."""
        q = _make_w2_question(pass_criteria="", is_hard_filter=True)
        result = _sanitize_pre_screen_questions_for_pair([q], boolean_mode=False)
        assert result[0]["is_hard_filter"] is True

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
