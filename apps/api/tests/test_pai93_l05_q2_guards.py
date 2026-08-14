"""Regression tests for PAI-93 L0.5 role-question suppression guards."""

import os

for _proxy_key in (
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
):
    os.environ.pop(_proxy_key, None)

from routers.campaigns import _normalize_questions_for_screening_level
from routers.engagement import _enforce_boolean_pre_screen_questions


def _questions_fixture():
    return [
        {
            "question_text": "Are you open to exploring new job opportunities?",
            "category": "default",
            "order_index": 0,
            "is_hard_filter": True,
        },
        {
            "question_text": "What is your current or most recent role and key responsibilities?",
            "category": "default",
            "order_index": 1,
            "is_hard_filter": False,
        },
        {
            "question_text": "What is your current location?",
            "category": "default",
            "order_index": 2,
            "is_hard_filter": False,
        },
    ]


def test_normalize_questions_l05_removes_role_question_and_reindexes():
    normalized = _normalize_questions_for_screening_level(_questions_fixture(), "L0.5")

    assert len(normalized) == 2
    assert all("current or most recent role" not in (q.get("question_text") or "").lower() for q in normalized)
    assert [q.get("order_index") for q in normalized] == [0, 1]


def test_normalize_questions_non_l05_keeps_role_question():
    normalized = _normalize_questions_for_screening_level(_questions_fixture(), "L1.5")

    assert len(normalized) == 3
    assert any("current or most recent role" in (q.get("question_text") or "").lower() for q in normalized)


def test_enforce_boolean_questions_removes_role_question_and_rewrites_role_specific():
    questions = [
        {
            "question_text": "What is your current or most recent role and key responsibilities?",
            "category": "default",
            "order_index": 1,
            "is_hard_filter": False,
        },
        {
            "question_text": "Describe your hands-on experience with SAP SD",
            "category": "role-specific",
            "order_index": 5,
            "is_hard_filter": False,
        },
        {
            "question_text": "What is your current location?",
            "category": "default",
            "order_index": 2,
            "is_hard_filter": False,
        },
    ]

    rewritten = _enforce_boolean_pre_screen_questions(questions)

    assert all("current or most recent role" not in (q.get("question_text") or "").lower() for q in rewritten)
    role_specific = next(q for q in rewritten if (q.get("category") or "").lower() == "role-specific")
    assert role_specific["question_text"].lower().startswith("do you ")
    assert role_specific["is_hard_filter"] is True
    assert any("current location" in (q.get("question_text") or "").lower() for q in rewritten)


def test_enforce_boolean_questions_preserves_work_arrangement():
    # PAI-96: work-arrangement is a yes/no hard filter — always passed through unchanged.
    q = {
        "question_text": "This role follows a hybrid work arrangement based in Minneapolis. Are you open to working in this setup?",
        "category": "work-arrangement",
        "order_index": 2,
        "is_hard_filter": True,
    }

    rewritten = _enforce_boolean_pre_screen_questions([q])
    assert len(rewritten) == 1
    assert rewritten[0]["question_text"] == q["question_text"]
