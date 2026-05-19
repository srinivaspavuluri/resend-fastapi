"""
Resend service — the ONLY place in the codebase that calls Resend's API.
Everything else (contacts, segments, customers) lives in our own database.

Resend handles:
  - Domain verification
  - Email delivery (single + batch)
  - Webhook events

We handle:
  - Which contacts belong to which customer
  - Segmentation and filtering
  - Contact subscriptions
"""
import os
import resend
from dataclasses import dataclass
from typing import List
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY", "")


@dataclass
class EmailRecipient:
    email: str
    first_name: str = ""


# ── Domain management ─────────────────────────────────────────────────────────

def add_domain(domain_name: str) -> dict:
    """
    Register a customer's domain with Resend.
    Returns the domain ID and DNS records to give to the customer.
    The customer must add these DNS records to their registrar before
    verification can succeed.
    """
    response = resend.Domains.create({"name": domain_name})
    return response


def verify_domain(domain_id: str) -> dict:
    """
    Trigger domain verification.
    Call this after the customer says they've added the DNS records.
    DNS propagation can take up to 48 hours — poll /status to check.
    """
    response = resend.Domains.verify(domain_id)
    return response


def get_domain_status(domain_id: str) -> dict:
    """
    Check the current verification status of a domain.
    Possible statuses: not_started, pending, verified, failed
    """
    response = resend.Domains.get(domain_id)
    return response


# ── Email sending ─────────────────────────────────────────────────────────────

def send_single(from_domain: str, to_email: str, subject: str, html: str) -> dict:
    """
    Send a single transactional email.
    Use for: password resets, notifications, confirmations.
    """
    params = {
        "from": f"hello@{from_domain}",
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    return resend.Emails.send(params)


def _build_batch(
    from_domain: str,
    recipients: List[EmailRecipient],
    subject: str,
    html_template: str
) -> List[dict]:
    """Build a list of email dicts, personalising {{first_name}} per recipient."""
    return [
        {
            "from": f"hello@{from_domain}",
            "to": recipient.email,
            "subject": subject.replace("{{first_name}}", recipient.first_name or "there"),
            "html": html_template.replace("{{first_name}}", recipient.first_name or "there"),
        }
        for recipient in recipients
    ]


def send_batch(
    from_domain: str,
    recipients: List[EmailRecipient],
    subject: str,
    html_template: str
) -> dict:
    """
    Send up to 100 emails in one Resend API call.
    Resend's batch endpoint limit is 100 per request.
    """
    if len(recipients) > 100:
        raise ValueError("Batch size cannot exceed 100. Use send_bulk() for larger lists.")
    emails = _build_batch(from_domain, recipients, subject, html_template)
    return resend.Batch.send(emails)


def send_bulk(
    from_domain: str,
    recipients: List[EmailRecipient],
    subject: str,
    html_template: str
) -> List[dict]:
    """
    Send to any number of recipients by automatically splitting into
    batches of 100. Returns a list of responses (one per batch).
    """
    results = []
    for i in range(0, len(recipients), 100):
        chunk = recipients[i : i + 100]
        result = send_batch(from_domain, chunk, subject, html_template)
        results.append(result)
    return results
