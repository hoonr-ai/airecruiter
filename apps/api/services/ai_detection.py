"""AI-generated-content detection for resumes ("AI plagiarism check").

Scores how likely a resume's prose was written by an LLM rather than a
person, with cited evidence for both directions. Powers POST /tira/ai-check
(bulk upload from the Tira panel).

Detection from text alone is inherently probabilistic — the prompt is
calibrated to prefer "uncertain" over confident wrong answers, and the UI
presents results as a heuristic signal, not proof.
"""
from __future__ import annotations

import logging
import time
from typing import List, Literal

from pydantic import BaseModel, Field

from core import llm_cache
from core.llm_client import get_openai_client, log_usage, model_for

logger = logging.getLogger(__name__)

# 7 days — the same resume re-checked yields the same verdict, and recruiters
# re-run overlapping batches while working through a pipeline.
_AI_DETECT_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

# Resumes rarely exceed a few thousand chars; the cap keeps latency and cost
# predictable for pathological inputs (e.g. a 60-page CV export).
_MAX_RESUME_CHARS = 20_000


class ResumeAIDetection(BaseModel):
    """Structured verdict for one resume."""

    ai_likelihood: int = Field(
        description="0-100 likelihood the resume prose is AI-generated. 0-29 likely human, 30-64 uncertain, 65-100 likely AI."
    )
    verdict: Literal["likely_human", "uncertain", "likely_ai"]
    confidence: Literal["low", "medium", "high"]
    ai_signals: List[str] = Field(
        description="Up to 6 concrete signs of AI generation, each citing evidence from the text."
    )
    human_signals: List[str] = Field(
        description="Up to 6 concrete signs of human authorship, each citing evidence from the text."
    )
    summary: str = Field(
        description="2-3 sentence plain-language assessment, including whether it reads fully AI-written, AI-polished, or human-written."
    )


_SYSTEM_PROMPT = """You are a document-forensics analyst who estimates whether a RESUME was written by an AI text generator, written by a person, or a person's draft polished by AI.

Weigh AI-generation signals such as:
- Buzzword clusters with no concrete anchor ("results-driven professional leveraging cutting-edge solutions").
- Uniform bullet rhythm: every bullet is verb + task + vague outcome, near-identical length and structure across all roles.
- Formulaic or suspiciously round metrics repeated across roles ("improved efficiency by 30%", "reduced costs by 25%") that don't fit the actual job.
- Absence of messy specifics: no product names, tool versions, team names, client industries, or internal project names anywhere.
- LLM-signature vocabulary in density: spearheaded, orchestrated, leveraged, honed, fostered, seamlessly, meticulous, showcasing, dynamic, delve.
- Summary/profile sections that read like a generic cover letter, detached from the work history below them.
- Skill lists that simply restate the bullets 1:1 or enumerate every trendy technology.

Weigh human-authorship signals such as:
- Hyper-specific, verifiable detail: version numbers, niche internal tools, odd non-round metrics ("cut deploy time from 43 to 12 minutes"), named products/teams.
- Idiosyncratic phrasing, abbreviations, inconsistent formatting or punctuation.
- Domain jargon used the way practitioners actually use it.
- A coherent career narrative with natural quirks and gaps.

Calibration rules:
- ai_likelihood: 0-29 → verdict likely_human, 30-64 → uncertain, 65-100 → likely_ai. The verdict MUST agree with the score band.
- Polish alone is NOT proof of AI — many strong resumes are professionally written or heavily edited. Do not flag skilled human writing just for being clean.
- Detection from text alone is probabilistic. On mixed evidence or short resumes (< 150 words), prefer "uncertain" with low or medium confidence. Never claim certainty.
- Each signal must cite concrete evidence (a short quoted fragment or a specific pattern you observed in THIS resume), not a generic possibility.
- In the summary, say which pattern it most resembles: fully AI-written, AI-polished human draft, or human-written."""


class AIDetectionService:
    def __init__(self):
        self.client = get_openai_client()

    async def detect(self, resume_text: str, filename: str = "") -> ResumeAIDetection:
        """Analyze one resume. Raises RuntimeError when the model is
        unavailable or the call fails — bulk callers bucket that file as
        failed instead of fabricating a neutral verdict."""
        if not self.client:
            raise RuntimeError("OpenAI isn't configured on the server (missing OPENAI_API_KEY).")

        text = (resume_text or "")[:_MAX_RESUME_CHARS]
        model = model_for("ai_detect", "gpt-4o-mini")

        cache_key = llm_cache.make_key("ai_detect", 1, model, text)
        cached = await llm_cache.get_json(cache_key)
        if cached is not None:
            try:
                verdict = ResumeAIDetection.model_validate(cached)
                logger.info("ai_detect: cache HIT")
                return verdict
            except Exception as exc:
                # Schema drift — drop the entry and re-run.
                logger.warning(f"ai_detect: cached verdict failed validation, re-running: {exc}")

        started = time.monotonic()
        try:
            completion = await self.client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"RESUME ({filename or 'unnamed'}):\n\n{text}"},
                ],
                response_format=ResumeAIDetection,
                temperature=0.2,
                prompt_cache_key="ai-detect-v1",
            )
        except Exception as e:
            logger.error(f"ai_detect: model call failed for {filename!r}: {e}")
            raise RuntimeError(f"Model call failed: {e}") from e

        log_usage("ai_detect", completion, duration_ms=(time.monotonic() - started) * 1000)
        verdict = completion.choices[0].message.parsed
        if verdict is None:
            raise RuntimeError("Model returned no parsed verdict.")

        # Clamp + keep verdict consistent with the score band even if the
        # model drifts from its calibration rules.
        verdict.ai_likelihood = max(0, min(100, int(verdict.ai_likelihood)))
        band = (
            "likely_ai" if verdict.ai_likelihood >= 65
            else "uncertain" if verdict.ai_likelihood >= 30
            else "likely_human"
        )
        if verdict.verdict != band:
            verdict.verdict = band

        try:
            await llm_cache.set_json(cache_key, verdict.model_dump(), ttl_seconds=_AI_DETECT_CACHE_TTL_SECONDS)
        except Exception as cache_exc:
            logger.debug(f"ai_detect: cache set failed: {cache_exc}")
        return verdict


ai_detection_service = AIDetectionService()
