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
import httpx
from dataclasses import dataclass
from typing import List
from dotenv import load_dotenv

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY", "")

_RESEND_API_URL = "https://api.resend.com"


def _auth_headers(idempotency_key: str = "") -> dict:
    headers = {
        "Authorization": f"Bearer {resend.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


@dataclass
class EmailRecipient:
    email: str
    first_name: str = ""


@dataclass
class SendResult:
    email: str
    resend_email_id: str


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

async def send_single(
    from_domain: str,
    to_email: str,
    subject: str,
    html: str,
    idempotency_key: str = "",
) -> dict:
    """
    Send a single transactional email.
    Pass idempotency_key to make retries safe — Resend deduplicates on their
    side if the same key arrives twice with the same payload.
    """
    payload = {
        "from": f"hello@{from_domain}",
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_RESEND_API_URL}/emails",
            json=payload,
            headers=_auth_headers(idempotency_key),
        )
        resp.raise_for_status()
        return resp.json()


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


async def send_batch(
    from_domain: str,
    recipients: List[EmailRecipient],
    subject: str,
    html_template: str,
    idempotency_key: str = "",
) -> List[SendResult]:
    """
    Send up to 100 emails in one Resend API call.
    Returns one SendResult per recipient, in the same order, each holding the
    resend_email_id that identifies that specific delivery on the webhook side.

    The idempotency_key is sent as an Idempotency-Key header — Resend deduplicates
    on their side if the same key arrives again with the same payload.
    """
    if len(recipients) > 100:
        raise ValueError("Batch size cannot exceed 100.")
    payload = _build_batch(from_domain, recipients, subject, html_template)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_RESEND_API_URL}/emails/batch",
            json=payload,
            headers=_auth_headers(idempotency_key),
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    return [
        SendResult(email=recipients[i].email, resend_email_id=data[i].get("id", ""))
        for i in range(len(data))
    ]
