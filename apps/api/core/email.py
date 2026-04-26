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
PAIR_TEAM_EMAIL = _cfg("PAIR_TEAM_EMAIL", "pair-recruiting@pyramidci.com")
APP_BASE_URL    = _cfg("APP_BASE_URL", "https://qacurate.hoonr.ai")
JOBDIVA_URL     = _cfg("JOBDIVA_URL", "https://www1.jobdiva.com")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _smtp_configured() -> bool:
    """Return True only when enough SMTP settings are present to attempt a send."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _send(to_addresses: List[str], subject: str, html_body: str, text_body: str = "") -> bool:
    """
    Low-level send helper.  Returns True on success, False on any failure
    (never raises so callers don't fail the primary request on email errors).
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
