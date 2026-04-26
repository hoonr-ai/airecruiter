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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read once at import time so hot-reload picks up changes)
# ---------------------------------------------------------------------------
def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _cfg_bool(key: str, default: bool = True) -> bool:
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


SMTP_HOST     = _cfg("SMTP_HOST")
SMTP_PORT     = int(_cfg("SMTP_PORT", "465"))
SMTP_USE_SSL  = _cfg_bool("SMTP_USE_SSL", True)
SMTP_USER     = _cfg("SMTP_USER")
SMTP_PASSWORD = _cfg("SMTP_PASSWORD")
SMTP_FROM     = _cfg("SMTP_FROM") or SMTP_USER  # same as Tira bug-report

# PAIR-specific (not SMTP credentials)
PAIR_TEAM_EMAIL    = _cfg("PAIR_TEAM_EMAIL",    "pair-recruiting@pyramidci.com")
JOB_POSTING_EMAIL  = _cfg("JOB_POSTING_EMAIL",  "Jobposting@pyramidci.com")
APP_BASE_URL       = _cfg("APP_BASE_URL",        "https://qacurate.hoonr.ai")
JOBDIVA_URL        = _cfg("JOBDIVA_URL",         "https://www1.jobdiva.com")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _smtp_configured() -> bool:
    """Return True only when enough SMTP settings are present to attempt a send."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _send(
    to_addresses: List[str],
    subject: str,
    html_body: str,
    text_body: str = "",
    attachments: Optional[List[dict]] = None  # List of {"filename": str, "content": bytes}
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
        from email.mime.application import MIMEApplication
        for att in attachments:
            filename = att.get("filename", "attachment")
            content  = att.get("content")
            if not content:
                continue
            part = MIMEApplication(content)
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
) -> bool:
    """
    Email #1 – PAIR Launch Confirmation.

    From : pair@pyramidci.com
    To   : pair-recruiting@pyramidci.com + recruiter emails
    Subj : PAIR Has Been Launched for [jobdiva_id]
    """
    jobdiva_link   = f"{JOBDIVA_URL}/jobdiva/servlet/jd?uid={jobdiva_id}"
    rankings_link  = f"{APP_BASE_URL}/jobs/{job_id}/rankings"

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
        """Convert **bold**, *italic*, and [label](url) to HTML spans."""
        # Links first so inner text isn't mangled
        text = _re.sub(
            r'\[([^\]]+)\]\(([^)]+)\)',
            r'<a href="\2" target="_blank" style="color:#4f46e5;text-decoration:underline;">\1</a>',
            text,
        )
        # Bold
        text = _re.sub(r'\*\*(.+?)\*\*', r'<strong style="font-weight:600;color:#1e293b;">\1</strong>', text)
        # Italic (single *)
        text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
        return text

    def _render_description(raw: str) -> str:
        """Render the full ai_description as HTML matching the UI component."""
        if not raw or not raw.strip():
            return "<em style='color:#94a3b8;'>Not available</em>"

        lines = raw.split("\n")
        html_parts: list = []

        for line in lines:
            trimmed = line.strip()

            # Empty line → spacer
            if not trimmed:
                html_parts.append('<div style="height:8px;"></div>')
                continue

            # Header detection: **ALL CAPS** or plain ALL CAPS (3–25 chars)
            is_header = (
                bool(_re.match(r'^\*\*[A-Z\s]+\*\*$', trimmed))
                or bool(_re.match(r'^[A-Z\s]{3,25}$', trimmed))
            )
            if is_header:
                title = trimmed.replace("**", "").strip()
                html_parts.append(
                    f'<div style="font-size:14px;font-weight:700;color:#0f172a;'
                    f'margin-top:20px;margin-bottom:6px;text-transform:uppercase;'
                    f'letter-spacing:0.04em;">{title}</div>'
                )
                continue

            # Bullet points (• or -)
            if trimmed.startswith("•") or trimmed.startswith("-"):
                content = _re.sub(r'^[•\-]\s*', '', trimmed)
                html_parts.append(
                    f'<div style="display:flex;gap:10px;margin-left:4px;'
                    f'margin-top:4px;margin-bottom:4px;align-items:flex-start;">'
                    f'<span style="color:#94a3b8;margin-top:2px;">•</span>'
                    f'<div style="flex:1;font-size:13px;color:#334155;">'
                    f'{_render_inline(content)}</div></div>'
                )
                continue

            # Normal paragraph line
            html_parts.append(
                f'<div style="margin-bottom:6px;font-size:13px;color:#475569;line-height:1.75;">'
                f'{_render_inline(trimmed)}</div>'
            )

        return "\n".join(html_parts)

    desc_html  = _render_description(ai_description or "")
    desc_plain = (ai_description or "—").strip()

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
                padding:16px;margin-bottom:8px;">
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
        f"Posting Description:\n{desc_plain}\n"
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
) -> bool:
    """
    Email #3 – Candidate Passed Phone Screen.

    From : pair@pyramidci.com
    To   : pair-recruiting@pyramidci.com + recruiter emails
    Subj : [Candidate Name] – Passed Phone Screen for [jobdiva_id]
    """
    jobdiva_link   = f"{JOBDIVA_URL}/jobdiva/servlet/jd?uid={jobdiva_id}"
    rankings_link  = f"{APP_BASE_URL}/jobs/{job_id}/rankings"
    report_link    = f"{APP_BASE_URL}/jobs/{job_id}/candidates/{candidate_id}"

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

    # Screening Summary Rows
    summary_rows_html = ""
    for item in screening_summary:
        f = item.get("field") or "—"
        v = item.get("value") or "—"
        summary_rows_html += _info_row(f, v)

    content = f"""
    <h2 style="margin:0 0 6px;font-size:20px;color:#1e293b;">
      ✅ Candidate Passed Phone Screen
    </h2>
    <p style="margin:0 0 20px;font-size:14px;color:#64748b;">
      Great news! <strong>{candidate_name}</strong> has successfully cleared the initial screening.
    </p>

    <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#64748b;
              text-transform:uppercase;letter-spacing:0.05em;">Job Details</p>
    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:20px;">
      <tbody>
        {_info_row("Job Diva ID", jd_hyperlink)}
        {_info_row("Job Title", rankings_hyperlink)}
        {_info_row("Location", location or "—")}
        {_info_row("Salary Range", salary_range or "—")}
      </tbody>
    </table>

    <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#64748b;
              text-transform:uppercase;letter-spacing:0.05em;">Candidate Details</p>
    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:20px;">
      <tbody>
        {_info_row("Name", candidate_name)}
        {_info_row("Email", candidate_email or "—")}
        {_info_row("Phone", candidate_phone or "—")}
        {_info_row("Screen Score", f"<strong>{screen_score}</strong>" if screen_score else "—")}
      </tbody>
    </table>

    <p style="margin:0 0 6px;font-size:13px;font-weight:600;color:#64748b;
              text-transform:uppercase;letter-spacing:0.05em;">Summary</p>
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                padding:16px;font-size:13px;color:#334155;line-height:1.7;margin-bottom:20px;">
      {summary or "No summary available."}
    </div>

    <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#64748b;
              text-transform:uppercase;letter-spacing:0.05em;">Screening Summary</p>
    <table cellpadding="0" cellspacing="0"
           style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                  width:100%;margin-bottom:24px;">
      <tbody>
        {summary_rows_html if summary_rows_html else '<tr><td style="padding:12px;color:#94a3b8;font-style:italic;">No detailed screening fields.</td></tr>'}
      </tbody>
    </table>

    <p style="margin:24px 0 12px;text-align:center;">
      <span style="font-size:13px;color:#64748b;display:block;margin-bottom:12px;">
        View Full Candidate Report (Coming Soon)
      </span>
      {_btn("#", "View Full Report", color="#94a3b8")}
    </p>
    """

    to_list = list(dict.fromkeys(
        [PAIR_TEAM_EMAIL] + [e.strip() for e in recruiter_emails if e.strip()]
    ))

    subject = f"{candidate_name} – Passed Phone Screen for {jobdiva_id}"

    plain = (
        f"{candidate_name} – Passed Phone Screen for {jobdiva_id}\n\n"
        f"Job Details:\n"
        f"Job Diva ID: {jobdiva_id} ({jobdiva_link})\n"
        f"Job Title: {job_title}\n"
        f"Location: {location}\n"
        f"Salary: {salary_range}\n\n"
        f"Candidate Details:\n"
        f"Name: {candidate_name}\n"
        f"Email: {candidate_email}\n"
        f"Phone: {candidate_phone}\n"
        f"Screen Score: {screen_score}\n\n"
        f"Summary:\n{summary}\n\n"
        f"Report: {report_link}\n"
    )

    attachments = []
    if resume_bytes and resume_filename:
        attachments.append({"filename": resume_filename, "content": resume_bytes})

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
    <h2 style="margin:0 0 6px;font-size:20px;color:#1e293b;">
      ⏸️ PAIR Is Now Inactive
    </h2>
    <p style="margin:0 0 20px;font-size:14px;color:#334155;line-height:1.7;">
      Please note that PAIR’s activity is halted for {jd_hyperlink}. 
      While inactive, PAIR stops candidate outreach.
    </p>

    <div style="background:#fff7ed;border-left:4px solid #f97316;padding:16px;margin-bottom:24px;">
      <p style="margin:0;font-size:14px;color:#9a3412;font-weight:600;">
        Job posting team, please close external postings related to this job.
      </p>
    </div>

    <p style="margin:0 0 16px;font-size:13px;color:#64748b;line-height:1.6;font-style:italic;">
      <strong>Note:</strong> A job may be marked as inactive in PAIR either manually 
      by a recruiter or automatically when its status in Job Diva is set to 
      Closed, Filled, Canceled, Ignored, Declined, or Expired. 
      PAIR cannot be restarted for the job unless the JobDiva status is Open or On Hold.
    </p>

    <div style="border-top:1px solid #e2e8f0;padding-top:16px;margin-top:24px;">
      <p style="margin:0;font-size:14px;color:#475569;">
        To relaunch PAIR, navigate to the <strong>Jobs List</strong>, and click 
        <strong>Edit Job Configuration</strong> under Actions.
      </p>
    </div>
    """

    # Combined TO list
    to_list = list(dict.fromkeys(
        [PAIR_TEAM_EMAIL, JOB_POSTING_EMAIL] + [e.strip() for e in recruiter_emails if e.strip()]
    ))

    subject = f"PAIR Is Now Inactive for {jobdiva_id}"

    plain = (
        f"PAIR Is Now Inactive for {jobdiva_id}\n\n"
        f"Please note that PAIR’s activity is halted for {jobdiva_id} ({jobdiva_link}). "
        f"While inactive, PAIR stops candidate outreach.\n\n"
        f"Job posting team, please close external postings related to this job.\n\n"
        f"Note: A job may be marked as inactive in PAIR either manually by a recruiter or "
        f"automatically when its status in Job Diva is set to Closed, Filled, Canceled, Ignored, Declined, or Expired. "
        f"PAIR cannot be restarted for the job unless the JobDiva status is Open or On Hold.\n\n"
        f"To relaunch PAIR, navigate to the Jobs List, and click Edit Job Configuration under Actions.\n"
    )

    return _send(to_list, subject, _base_html(content), plain)
