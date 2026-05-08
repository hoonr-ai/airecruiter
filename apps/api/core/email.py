"""
PAIR Email Notification Service
================================
Centralised module for all outbound email notifications triggered by PAIR
workflow events.  Notifications are sent via SMTP (configured through env vars
that already exist for the Tira bug-report flow).

Environment variables (set in .env / Azure App Settings):
    SMTP_HOST          – mail server host
    SMTP_PORT          – port (default 465)
    SMTP_USE_SSL       – "true" / "false" (default true for port 465)
    SMTP_USER          – login / sender credential
    SMTP_PASSWORD      – login password
    SMTP_FROM          – From address shown to recipients (falls back to SMTP_USER)

    PAIR_TEAM_EMAIL    – fixed team inbox; default pair-recruiting@pyramidci.com
    APP_BASE_URL       – public front-end URL used to build deep-links
                         (e.g. https://qacurate.hoonr.ai)
    JOBDIVA_URL        – JobDiva instance root (default https://www1.jobdiva.com)
"""

import os
import json
import logging
import smtplib
import ssl
import html
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read once at import time so hot-reload picks up changes)
# ---------------------------------------------------------------------------
def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()




SMTP_HOST     = _cfg("SMTP_HOST")
SMTP_PORT     = int(_cfg("SMTP_PORT", "587"))
SMTP_USE_SSL  = (SMTP_PORT == 465)
SMTP_USER     = _cfg("SMTP_USER") or _cfg("EMAIL_FROM")
SMTP_PASSWORD = _cfg("SMTP_PASSWORD") or _cfg("EMAIL_PASSWORD")
SMTP_FROM     = _cfg("SMTP_FROM") or _cfg("EMAIL_FROM") or SMTP_USER

# PAIR-specific (not SMTP credentials)
PAIR_TEAM_EMAIL    = _cfg("PAIR_TEAM_EMAIL",    "pair-recruiting@pyramidci.com")
JOB_POSTING_EMAIL  = _cfg("JOB_POSTING_EMAIL",  "Jobposting@pyramidci.com")
APP_BASE_URL       = _cfg("APP_BASE_URL",        "https://qacurate.hoonr.ai")
JOBDIVA_URL        = _cfg("JOBDIVA_URL",         "https://www1.jobdiva.com")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def resolve_app_base_url(override: Optional[str] = None) -> str:
    """Prefer the caller's current frontend origin over the env default."""
    candidate = (override or "").strip().rstrip("/")
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return candidate
    return APP_BASE_URL.rstrip("/")

def _smtp_configured() -> bool:
    """Return True only when enough SMTP settings are present to attempt a send."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _send(
    to_addresses: List[str],
    subject: str,
    html_body: str,
    text_body: str = "",
    attachments: Optional[List[dict]] = None  # List of {"filename": str, "content": bytes, "content_type"?: str}
) -> bool:
    """
    Low-level send helper.  Returns True on success, False on any failure.
    """
    if not _smtp_configured():
        logger.warning(
            "📧 SMTP not configured — skipping email '%s' to %s",
            subject, to_addresses,
        )
        return False

    if not to_addresses:
        logger.warning("📧 No recipients — skipping email '%s'", subject)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"PAIR Recruiting <{SMTP_FROM}>"
    msg["To"]      = ", ".join(to_addresses)

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    # Attachments
    if attachments:
        for att in attachments:
            filename = att.get("filename", "attachment")
            content  = att.get("content")
            content_type = att.get("content_type", "application/octet-stream")
            if not content:
                continue
            main_type, sub_type = content_type.split("/", 1)
            part = MIMEBase(main_type, sub_type)
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

    try:
        if SMTP_USE_SSL:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, to_addresses, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, to_addresses, msg.as_string())

        logger.info("📧 Email sent: '%s' → %s", subject, to_addresses)
        return True

    except Exception as exc:
        logger.error("📧 Email send failed: %s", exc, exc_info=True)
        return False


def _base_html(content: str) -> str:
    """Wrap content in a simple, clean HTML email shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PAIR Notification</title>
</head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:10px;overflow:hidden;
                      box-shadow:0 2px 12px rgba(0,0,0,0.08);">
          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#4f46e5,#7c3aed);
                       padding:28px 36px;text-align:center;">
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-0.3px;">
                PAIR Recruiting
              </p>
              <p style="margin:4px 0 0;font-size:13px;color:rgba(255,255,255,0.75);">
                Automated Notification
              </p>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding:32px 36px 28px;">
              {content}
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="background:#f8fafc;padding:18px 36px;text-align:center;
                       border-top:1px solid #e2e8f0;">
              <p style="margin:0;font-size:12px;color:#94a3b8;line-height:1.6;">
                This is an automated message from the PAIR Recruiting platform.<br>
                Please do not reply directly to this email.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _btn(url: str, label: str, color: str = "#4f46e5") -> str:
    return (
        f'<a href="{url}" target="_blank" '
        f'style="display:inline-block;padding:11px 24px;background:{color};'
        f'color:#ffffff;text-decoration:none;border-radius:6px;'
        f'font-weight:600;font-size:14px;margin:8px 4px;">'
        f'{label}</a>'
    )


def _info_row(label: str, value: str) -> str:
    return (
        f'<tr>'
        f'<td style="padding:8px 12px;color:#64748b;font-size:13px;white-space:nowrap;">{label}</td>'
        f'<td style="padding:8px 12px;color:#1e293b;font-size:13px;font-weight:500;">{value}</td>'
        f'</tr>'
    )


def _build_word_resume_document(candidate_name: str, resume_text: str) -> bytes:
    """Return a Word-compatible HTML document as .doc bytes."""
    safe_name = html.escape(candidate_name or "Candidate")
    resume_html = html.escape(resume_text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    doc_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{safe_name} Resume</title>
</head>
<body style="font-family:Calibri, Arial, sans-serif;font-size:11pt;line-height:1.4;color:#111827;">
  <h1 style="font-size:16pt;margin:0 0 12pt;">{safe_name} Resume</h1>
  <p style="margin:0;white-space:normal;">{resume_html}</p>
</body>
</html>"""
    return doc_html.encode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_pair_launched(
    *,
    jobdiva_id: str,
    job_title: str,
    customer_name: str,
    candidate_count: int,
    recruiter_emails: List[str],
    job_id: str,                # internal DB job_id for deep-link
    app_base_url: Optional[str] = None,
) -> bool:
    """
    Email #1 – PAIR Launch Confirmation.

    From : pair@pyramidci.com
    To   : pair-recruiting@pyramidci.com + recruiter emails
    Subj : PAIR Has Been Launched for [jobdiva_id]
    """
    base_url = resolve_app_base_url(app_base_url)
    jobdiva_link   = f"{JOBDIVA_URL}/jobdiva/servlet/jd?uid={jobdiva_id}"
    rankings_link  = f"{base_url}/jobs/{job_id}/rankings"

    jd_hyperlink = (
        f'<a href="{jobdiva_link}" target="_blank" '
        f'style="color:#4f46e5;font-weight:600;text-decoration:none;">'
        f'{jobdiva_id}</a>'
    )

    content = f"""
    <h2 style="margin:0 0 6px;font-size:20px;color:#1e293b;">
      🚀 PAIR Has Been Launched
    </h2>
    <p style="margin:0 0 20px;font-size:14px;color:#64748b;">
      A new PAIR campaign has just been activated.
    </p>

    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:24px;">
      <tbody>
        {_info_row("JobDiva ID", jd_hyperlink)}
        {_info_row("Job Title", job_title or "—")}
        {_info_row("Customer", customer_name or "—")}
        {_info_row("Candidates Sourced", str(candidate_count))}
      </tbody>
    </table>

    <p style="margin:0 0 16px;font-size:14px;color:#334155;line-height:1.7;">
      Congratulations, PAIR has been launched for
      <strong>{candidate_count}</strong> candidate(s) for
      {jd_hyperlink}: <strong>{job_title}</strong> – <strong>{customer_name}</strong>.
    </p>

    <p style="margin:0 0 20px;font-size:14px;color:#334155;line-height:1.7;">
      Any candidates that apply for this job will also automatically be enrolled
      in PAIR. You can always add more candidates into the pipeline by sourcing
      additional candidates.
    </p>

    <p style="margin:0 0 24px;text-align:center;">
      {_btn(rankings_link, "Track PAIR Progress →")}
    </p>
    """

    to_list = list(dict.fromkeys(
        [PAIR_TEAM_EMAIL] + [e.strip() for e in recruiter_emails if e.strip()]
    ))

    subject = f"PAIR Has Been Launched for {jobdiva_id}"

    plain = (
        f"PAIR Has Been Launched for {jobdiva_id}\n\n"
        f"Job: {job_title} – {customer_name}\n"
        f"Candidates sourced: {candidate_count}\n"
        f"JobDiva: {jobdiva_link}\n"
        f"Track progress: {rankings_link}\n"
    )

    return _send(to_list, subject, _base_html(content), plain)


def notify_job_posting(
    *,
    jobdiva_id: str,
    job_title: str,
    recruiter_emails: List[str],
    job_boards: List[str],
    ai_description: str,
    app_base_url: Optional[str] = None,
) -> bool:
    """
    Email #2 – Job Posting Request.

    From : SMTP_FROM
    To   : Jobposting@pyramidci.com, pair-recruiting@pyramidci.com + recruiter emails
    Subj : New Job Posting Request Received

    The posting description is rendered with the same logic as the UI's
    AIPostingJobDescription component (markdown-like → HTML).
    """
    import re as _re

    _ = resolve_app_base_url(app_base_url)
    jobdiva_link = f"{JOBDIVA_URL}/jobdiva/servlet/jd?uid={jobdiva_id}"

    jd_hyperlink = (
        f'<a href="{jobdiva_link}" target="_blank" '
        f'style="color:#4f46e5;font-weight:600;text-decoration:none;">'
        f'{jobdiva_id}</a>'
    )

    recruiter_list_html = ", ".join(recruiter_emails) if recruiter_emails else "—"

    # Job boards as a bulleted list or "—" if none
    if job_boards:
        boards_html = "<ul style='margin:4px 0 0 16px;padding:0;font-size:13px;color:#334155;'>" + \
            "".join(f"<li style='margin:2px 0;'>{b}</li>" for b in job_boards) + "</ul>"
        boards_plain = ", ".join(job_boards)
    else:
        boards_html = "<span style='color:#94a3b8;font-size:13px;'>—</span>"
        boards_plain = "—"

    # ── Markdown-to-HTML renderer matching the UI AIPostingJobDescription ──
    def _render_inline(text: str) -> str:
        """Convert [label](url), **bold**, and *italic* with semantic HTML."""
        if not text:
            return ""

        parts = _re.split(r'(\[.*?\]\(.*?\)+|\*\*.*?\*\*|\*(?!\*).*?\*(?!\*))', text)
        rendered: list[str] = []

        for part in parts:
            if not part:
                continue

            if part.startswith("[") and "](" in part and part.endswith(")"):
                match = _re.match(r'\[(.*?)\]\((.*?)\)', part)
                if match:
                    label = html.escape(match.group(1))
                    url = html.escape(match.group(2), quote=True)
                    rendered.append(
                        f'<a href="{url}" target="_blank" '
                        f'style="color:#4f46e5;text-decoration:underline;">{label}</a>'
                    )
                    continue

            if part.startswith("**") and part.endswith("**") and len(part) >= 4:
                rendered.append(
                    "<strong>"
                    f"{html.escape(part[2:-2])}"
                    "</strong>"
                )
                continue

            if part.startswith("*") and part.endswith("*") and len(part) >= 2:
                rendered.append(f"<em>{html.escape(part[1:-1])}</em>")
                continue

            rendered.append(html.escape(part))

        return "".join(rendered)

    def _render_copy_paste_text(raw: str) -> str:
        """Plain-text, copy/paste-friendly version of the posting description.

        Preserve paragraph breaks and literal bullets while removing markdown
        emphasis markers so the posting team can paste directly into job boards.
        """
        if not raw or not raw.strip():
            return "Not available"

        text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        text = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
        text = text.replace("**", "")
        text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', text)
        return text

    def _render_description(raw: str) -> str:
        """Render AI JD HTML optimized for manual select/copy from email clients."""
        if not raw or not raw.strip():
            return "<em style='color:#94a3b8;'>Not available</em>"

        lines = raw.split("\n")
        html_parts: list = []
        in_list = False

        def _close_list():
            nonlocal in_list
            if in_list:
                html_parts.append("</ul>")
                in_list = False

        for line in lines:
            trimmed = line.strip()

            # Empty line → spacer
            if not trimmed:
                _close_list()
                html_parts.append('<div style="height:8px;line-height:8px;">&nbsp;</div>')
                continue

            # Header detection: **ALL CAPS** or plain ALL CAPS (3–25 chars)
            is_header = (
                bool(_re.match(r'^\*\*[A-Z\s]+\*\*$', trimmed))
                or bool(_re.match(r'^[A-Z\s]{3,25}$', trimmed))
            )
            if is_header:
                _close_list()
                title = trimmed.replace("**", "").strip()
                html_parts.append(
                    f'<p style="margin:20px 0 6px 0;font-size:14px;line-height:1.5;'
                    f'color:#0f172a;text-transform:uppercase;letter-spacing:0.04em;">'
                    f'<strong>{html.escape(title)}</strong>'
                    f'</p>'
                )
                continue

            # Bullet points (• or -)
            if trimmed.startswith("•") or trimmed.startswith("-"):
                if not in_list:
                    html_parts.append(
                        "<ul style='margin:6px 0 10px 20px;padding:0;color:#334155;"
                        "font-size:13px;line-height:1.75;'>"
                    )
                    in_list = True
                content = _re.sub(r'^[•\-]\s*', '', trimmed)
                html_parts.append(
                    f"<li style='margin:0 0 4px 0;padding:0;'><span>{_render_inline(content)}</span></li>"
                )
                continue

            # Normal paragraph line
            _close_list()
            html_parts.append(
                f'<p style="margin:0 0 8px 0;font-size:13px;color:#475569;line-height:1.75;">'
                f'{_render_inline(trimmed)}</p>'
            )

        _close_list()
        return "\n".join(html_parts)

    desc_html  = _render_description(ai_description or "")
    desc_plain = _render_copy_paste_text(ai_description or "—")

    content = f"""
    <h2 style="margin:0 0 6px;font-size:20px;color:#1e293b;">
      📋 New Job Posting Request Received
    </h2>
    <p style="margin:0 0 20px;font-size:14px;color:#334155;line-height:1.7;">
      Job posting team, please post the below job on the selected job boards.
      Please <strong>reply all</strong> here once the posts are live.
    </p>

    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:24px;">
      <tbody>
        {_info_row("Job Diva ID", jd_hyperlink)}
        {_info_row("Job Title", job_title or "—")}
        {_info_row("Recruiter Requesting", recruiter_list_html)}
      </tbody>
    </table>

    <p style="margin:0 0 6px;font-size:13px;font-weight:600;color:#64748b;
              text-transform:uppercase;letter-spacing:0.05em;">Job Boards</p>
    <div style="margin-bottom:20px;">{boards_html}</div>

    <p style="margin:0 0 6px;font-size:13px;font-weight:600;color:#64748b;
              text-transform:uppercase;letter-spacing:0.05em;">Posting Description</p>
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                padding:16px;margin-bottom:8px;-webkit-user-select:text;
                user-select:text;">
      {desc_html}
    </div>
    """

    # Deduplicated To list: job posting team first, then PAIR team, then recruiters
    to_list = list(dict.fromkeys(
        [JOB_POSTING_EMAIL, PAIR_TEAM_EMAIL]
        + [e.strip() for e in recruiter_emails if e.strip()]
    ))

    subject = "New Job Posting Request Received"

    plain = (
        "New Job Posting Request Received\n\n"
        "Job posting team, please post the below job on selected job boards."
        " Please reply all here once posts are live.\n\n"
        f"Job Diva ID: {jobdiva_id}\n"
        f"JobDiva link: {jobdiva_link}\n"
        f"Job Title: {job_title or '—'}\n"
        f"Recruiter Requesting: {recruiter_list_html}\n"
        f"Job Boards: {boards_plain}\n\n"
        f"Posting Description:\n{desc_plain or '—'}\n"
    )

    return _send(to_list, subject, _base_html(content), plain)


def notify_candidate_passed(
    *,
    candidate_name: str,
    candidate_email: str,
    candidate_phone: str,
    screen_score: str,
    summary: str,
    screening_summary: List[Dict[str, str]],
    jobdiva_id: str,
    job_title: str,
    location: str,
    salary_range: str,
    recruiter_emails: List[str],
    resume_bytes: Optional[bytes] = None,
    resume_filename: Optional[str] = None,
    candidate_id: str = "",
    job_id: str = "",
    app_base_url: Optional[str] = None,
) -> bool:
    """
    Email #3 – Candidate Passed Phone Screen.

    Triggered when: Candidate PASS on all phone screen hard filters & >70% match score
    From : pair@pyramidci.com
    To   : Pair-recruiting@pyramidci.com + recruiter emails
    Subj : [Candidate Name] – Passed Phone Screen for [jobdiva_id]
    """
    base_url = resolve_app_base_url(app_base_url)
    jobdiva_link   = f"{JOBDIVA_URL}/jobdiva/servlet/jd?uid={jobdiva_id}"
    rankings_link  = f"{base_url}/jobs/{job_id}/rankings"
    # Deep link to the candidate evaluation report
    report_link    = f"{base_url}/jobs/{job_id}/report?candidateId={candidate_id}"

    jd_hyperlink = (
        f'<a href="{jobdiva_link}" target="_blank" '
        f'style="color:#4f46e5;font-weight:600;text-decoration:none;">'
        f'{jobdiva_id}</a>'
    )
    rankings_hyperlink = (
        f'<a href="{rankings_link}" target="_blank" '
        f'style="color:#4f46e5;font-weight:600;text-decoration:none;">'
        f'{job_title or "PAIR Rank List"}</a>'
    )

    # Screening Summary rows rendered as stacked cards for readability.
    summary_rows_html = ""
    for item in screening_summary:
        f = item.get("field") or "—"
        v = item.get("value") or "—"
        field_text = html.escape(str(f))
        value_text = html.escape(str(v)).replace("\n", "<br>")

        # Color code Pass/Fail in the summary table
        val_styled = value_text
        v_low = str(v).lower()
        if "pass" in v_low:
            val_styled = f'<span style="color:#059669;font-weight:600;">{value_text}</span>'
        elif "fail" in v_low:
            val_styled = f'<span style="color:#dc2626;font-weight:600;">{value_text}</span>'

        summary_rows_html += (
            '<tr><td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;vertical-align:top;">'
            '<div style="font-size:12px;font-weight:700;color:#475569;margin-bottom:6px;line-height:1.4;">'
            f'{field_text}'
            '</div>'
            '<div style="font-size:13px;color:#1e293b;line-height:1.55;word-break:break-word;overflow-wrap:anywhere;">'
            f'{val_styled}'
            '</div>'
            '</td></tr>'
        )

    content = f"""
    <h2 style="margin:0 0 6px;font-size:20px;color:#1e293b;">
      ✅ Candidate Passed Phone Screen
    </h2>
    <p style="margin:0 0 20px;font-size:14px;color:#64748b;">
      Great news! <strong>{candidate_name}</strong> has successfully cleared the initial screening criteria.
    </p>

    <p style="margin:0 0 8px;font-size:12px;font-weight:600;color:#64748b;
               text-transform:uppercase;letter-spacing:0.05em;">Job Details</p>
    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:20px;">
      <tbody>
        {_info_row("JobDiva ID", jd_hyperlink)}
        {_info_row("Job Title", rankings_hyperlink)}
        {_info_row("Location", location or "—")}
        {_info_row("Salary Range", salary_range or "—")}
      </tbody>
    </table>

    <p style="margin:0 0 8px;font-size:12px;font-weight:600;color:#64748b;
               text-transform:uppercase;letter-spacing:0.05em;">Candidate Details</p>
    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:20px;">
      <tbody>
        {_info_row("Name", candidate_name)}
        {_info_row("Email", candidate_email or "—")}
        {_info_row("Phone", candidate_phone or "—")}
        {_info_row("Screen Score", f'<span style="font-size:15px;color:#4f46e5;font-weight:700;">{screen_score}</span>' if screen_score else "—")}
      </tbody>
    </table>

    <p style="margin:0 0 8px;font-size:12px;font-weight:600;color:#64748b;
               text-transform:uppercase;letter-spacing:0.05em;">Screening Summary</p>
    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:24px;">
      <tbody>
        {summary_rows_html if summary_rows_html else '<tr><td style="padding:12px;color:#94a3b8;font-style:italic;">No detailed screening fields.</td></tr>'}
      </tbody>
    </table>

    <p style="margin:0 0 24px;text-align:center;">
      {_btn(report_link, "View Full Candidate Report →")}
    </p>

    <div style="background:#fff7ed;border:1px solid #ffedd5;border-radius:8px;padding:12px;">
      <p style="margin:0;font-size:12px;color:#9a3412;line-height:1.5;">
        <strong>Note:</strong> The candidate's resume is attached in Word format (if available in JobDiva).
      </p>
    </div>
    """

    to_list = list(dict.fromkeys(
        [PAIR_TEAM_EMAIL] + [e.strip() for e in recruiter_emails if e.strip()]
    ))

    subject = f"{candidate_name} – Passed Phone Screen for {jobdiva_id}"

    plain = (
        f"{candidate_name} – Passed Phone Screen for {jobdiva_id}\n\n"
        f"Job Details:\n"
        f"Job Diva ID: {jobdiva_id} ({jobdiva_link})\n"
        f"Job Title: {job_title} ({rankings_link})\n"
        f"Location: {location}\n"
        f"Salary Range: {salary_range}\n\n"
        f"Candidate Details:\n"
        f"Name: {candidate_name}\n"
        f"Email: {candidate_email}\n"
        f"Phone: {candidate_phone}\n"
        f"Screen Score: {screen_score}\n\n"
        f"View Full Report: {report_link}\n"
    )

    attachments = []
    if resume_bytes and resume_filename:
        attachments.append({
            "filename": resume_filename,
            "content": resume_bytes,
            "content_type": "application/msword",
        })

    return _send(to_list, subject, _base_html(content), plain, attachments=attachments)



def notify_pair_inactive(
    *,
    jobdiva_id: str,
    recruiter_emails: List[str],
) -> bool:
    """
    Email #4 – PAIR Is Now Inactive.

    Triggered when PAIR status is updated to Inactive (manual or JobDiva sync).
    """
    jobdiva_link = f"{JOBDIVA_URL}/jobdiva/servlet/jd?uid={jobdiva_id}"
    
    jd_hyperlink = (
        f'<a href="{jobdiva_link}" target="_blank" '
        f'style="color:#4f46e5;text-decoration:none;font-weight:600;">'
        f'{jobdiva_id}</a>'
    )

    content = f"""
    <p style="margin:0 0 16px;font-size:14px;color:#334155;line-height:1.6;">
      Please note that PAIR’s activity is halted for {jd_hyperlink}. 
      While inactive, PAIR stops candidate outreach.
    </p>
    <p style="margin:0 0 16px;font-size:14px;color:#334155;line-height:1.6;font-weight:600;">
      Job posting team, please close external postings related to this job.
    </p>
    <p style="margin:0 0 16px;font-size:13px;color:#64748b;line-height:1.6;">
      Note: A job may be marked as inactive in PAIR either manually by a recruiter or 
      automatically when its status in Job Diva is set to Closed, Filled, Canceled, Ignored, Declined, or Expired. 
      PAIR cannot be restarted for the job unless the JobDiva status is Open or On Hold.
    </p>
    <p style="margin:0;font-size:13px;color:#64748b;line-height:1.6;">
      To relaunch PAIR, navigate to the Jobs List, and click <strong>Edit Job Configuration</strong> under Actions.
    </p>
    """

    # Combined TO list
    to_list = list(dict.fromkeys(
        [PAIR_TEAM_EMAIL, "Jobposting@pyramidci.com"] + [e.strip() for e in recruiter_emails if e.strip()]
    ))

    subject = f"PAIR Is Now Inactive for {jobdiva_id}"

    plain = (
        f"Please note that PAIR’s activity is halted for {jobdiva_id} ({jobdiva_link}). "
        f"While inactive, PAIR stops candidate outreach.\n\n"
        f"Job posting team, please close external postings related to this job.\n\n"
        f"Note: A job may be marked as inactive in PAIR either manually by a recruiter or "
        f"automatically when its status in Job Diva is set to Closed, Filled, Canceled, Ignored, Declined, or Expired. "
        f"PAIR cannot be restarted for the job unless the JobDiva status is Open or On Hold.\n\n"
        f"To relaunch PAIR, navigate to the Jobs List, and click Edit Job Configuration under Actions.\n"
    )

    return _send(to_list, subject, _base_html(content), plain)
