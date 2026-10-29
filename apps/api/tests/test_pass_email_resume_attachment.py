"""Coverage for `_resolve_pass_email_resume_attachment` (PR #867 review):
the Candidate Passed email should prefer the original resume file fetched
from JobDiva and only fall back to a generated .doc (built from parsed
resume text) when that file isn't available — whether because the candidate
has no JobDiva resume at all, or because `get_resume_file` rejected/failed
to fetch it (bad data, oversized, network error, etc.).
"""
import asyncio
import unicodedata

from routers.engagement.provisioning import _resolve_pass_email_resume_attachment, _safe_filename_stem


def _run(coro):
    return asyncio.run(coro)


class _FakeJobDivaService:
    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    async def get_resume_file(self, resume_id):
        self.calls.append(resume_id)
        if self._raises:
            raise self._raises
        return self._result


def test_prefers_original_file_when_available():
    svc = _FakeJobDivaService(result={"filename": "Resume.pdf", "content": b"%PDF-1.7"})
    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, "123_1_1", used_local_resume_fallback=False, resume_text="some text",
            candidate_name="Jane Doe", job_id="456",
        )
    )
    assert resume_bytes == b"%PDF-1.7"
    assert filename == "Resume.pdf"
    assert content_type == "application/pdf"
    assert is_original is True
    assert svc.calls == ["123_1_1"]


def test_falls_back_to_generated_doc_when_using_local_resume_text():
    """When the resume text itself came from the local DB fallback (no real
    JobDiva resume to point at), we must not even attempt get_resume_file."""
    svc = _FakeJobDivaService(result={"filename": "Resume.pdf", "content": b"%PDF-1.7"})
    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, "123_1_1", used_local_resume_fallback=True, resume_text="local resume text",
            candidate_name="Jane Doe", job_id="456",
        )
    )
    assert svc.calls == []  # never called — not a real JobDiva resume_id
    assert resume_bytes is not None
    assert filename == "Resume_Jane_Doe_456.doc"
    assert content_type == "application/msword"
    assert is_original is False


def test_falls_back_to_generated_doc_when_no_resume_id():
    svc = _FakeJobDivaService(result={"filename": "Resume.pdf", "content": b"%PDF-1.7"})
    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, None, used_local_resume_fallback=False, resume_text="jobdiva text",
            candidate_name="Jane Doe", job_id="456",
        )
    )
    assert svc.calls == []
    assert filename == "Resume_Jane_Doe_456.doc"
    assert is_original is False


def test_falls_back_to_generated_doc_when_file_fetch_returns_none():
    svc = _FakeJobDivaService(result=None)
    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, "123_1_1", used_local_resume_fallback=False, resume_text="jobdiva text",
            candidate_name="Jane Doe", job_id="456",
        )
    )
    assert svc.calls == ["123_1_1"]
    assert filename == "Resume_Jane_Doe_456.doc"
    assert content_type == "application/msword"
    assert is_original is False


def test_falls_back_to_generated_doc_when_file_fetch_raises():
    svc = _FakeJobDivaService(raises=RuntimeError("boom"))
    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, "123_1_1", used_local_resume_fallback=False, resume_text="jobdiva text",
            candidate_name="Jane Doe", job_id="456",
        )
    )
    assert filename == "Resume_Jane_Doe_456.doc"
    assert is_original is False


def test_no_attachment_when_nothing_available():
    svc = _FakeJobDivaService(result=None)
    resume_bytes, filename, content_type, is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, None, used_local_resume_fallback=False, resume_text="",
            candidate_name="Jane Doe", job_id="456",
        )
    )
    assert resume_bytes is None
    assert filename is None
    assert content_type is None
    assert is_original is False


# --- _safe_filename_stem --------------------------------------------------
# A candidate with a non-ASCII name must not end up with a mangled or
# empty-looking generated-.doc filename (e.g. "Resume__456.doc").

def test_safe_filename_stem_keeps_accented_names():
    assert _safe_filename_stem("José García") == "José_García"


def test_safe_filename_stem_recombines_decomposed_accents():
    """A name holding a combining accent as its own codepoint (NFD — possible
    depending on where the name was entered/imported) must not have that
    mark silently dropped the way a plain isalnum() filter would."""
    decomposed = unicodedata.normalize("NFD", "José")
    assert decomposed != "José"  # sanity: actually decomposed, not a no-op
    assert _safe_filename_stem(decomposed) == "José"


def test_safe_filename_stem_falls_back_when_nothing_alnum_survives():
    assert _safe_filename_stem("🙂🙂🙂") == "Candidate"
    assert _safe_filename_stem("") == "Candidate"
    assert _safe_filename_stem(None) == "Candidate"


def test_falls_back_to_generated_doc_with_decomposed_accented_name():
    """End-to-end through _resolve_pass_email_resume_attachment: the
    generated .doc's filename must keep the candidate's name recognizable,
    not collapse to "Resume__456.doc"."""
    svc = _FakeJobDivaService(result=None)
    decomposed_name = unicodedata.normalize("NFD", "José García")
    _resume_bytes, filename, _content_type, _is_original = _run(
        _resolve_pass_email_resume_attachment(
            svc, None, used_local_resume_fallback=False, resume_text="jobdiva text",
            candidate_name=decomposed_name, job_id="456",
        )
    )
    assert filename == "Resume_José_García_456.doc"
