"""
Resend Webhook Handler

Resend uses Svix to deliver webhooks. Every event POST includes:
  - svix-id        : unique event ID
  - svix-timestamp : when the event was sent
  - svix-signature : HMAC signature to verify the payload is from Resend

Setup steps:
  1. Go to resend.com → Webhooks → Add endpoint
  2. Enter your public URL: https://yourdomain.com/webhooks/resend
  3. Copy the signing secret → add to .env as WEBHOOK_SECRET
  4. Select events to subscribe to (recommended: all email.* events)

Local development — forward webhooks to localhost:
  npm install -g svix
  npx svix-cli listen http://localhost:8000/webhooks/resend
"""
import os
import time
import hmac
import hashlib
import base64
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Request, HTTPException, Header
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from ..database import get_db
from ..models import CampaignRecipient, Contact

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

logger = logging.getLogger(__name__)

# Read secret per-request (see _get_secret()) so runtime env changes are picked up.
# Emit a startup-time advisory if the secret is not configured.
_SECRET_AT_IMPORT = os.getenv("WEBHOOK_SECRET", "")
if not _SECRET_AT_IMPORT:
    logger.warning(
        "WEBHOOK_SECRET is not set — webhook signature verification is DISABLED. "
        "Set this in .env before deploying to production."
    )


def _get_secret() -> str:
    """Return the current WEBHOOK_SECRET from the environment (read per-request)."""
    return os.getenv("WEBHOOK_SECRET", "")


def _verify_svix_signature(
    payload: bytes,
    svix_id: str,
    svix_timestamp: str,
    svix_signature: str,
    secret: str
) -> bool:
    """
    Verify Resend/Svix webhook signature using HMAC-SHA256.
    Svix secrets are prefixed with 'whsec_' and base64 encoded.
    Signed content format: "{svix_id}.{svix_timestamp}.{raw_payload}"
    """
    try:
        if abs(int(time.time()) - int(svix_timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    if secret.startswith("whsec_"):
        secret_bytes = base64.b64decode(secret[6:])
    else:
        secret_bytes = secret.encode()

    signed_content = f"{svix_id}.{svix_timestamp}.".encode() + payload
    expected_digest = hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected_digest).decode()

    # svix_signature can contain multiple space-separated entries like "v1,<base64>"
    for sig in svix_signature.split(" "):
        if sig.startswith("v1,"):
            if hmac.compare_digest(expected_b64, sig[3:]):
                return True
    return False


@router.post("/resend", summary="Receive Resend delivery event webhooks")
async def handle_resend_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    svix_id: Optional[str] = Header(None, alias="svix-id"),
    svix_timestamp: Optional[str] = Header(None, alias="svix-timestamp"),
    svix_signature: Optional[str] = Header(None, alias="svix-signature"),
):
    """
    Receives delivery event notifications from Resend.

    Resend calls this endpoint automatically whenever an email changes state.
    You register this URL once in the Resend dashboard under **Webhooks**.

    ---

    **Events handled:**

    | Event type          | What it means                                      | Action taken              |
    |---------------------|----------------------------------------------------|---------------------------|
    | `email.sent`        | Email left Resend's servers                        | Log / update status       |
    | `email.delivered`   | Confirmed delivered to recipient's mail server     | Log / update status       |
    | `email.opened`      | Recipient opened the email (via tracking pixel)    | Log open event            |
    | `email.clicked`     | Recipient clicked a link in the email              | Log click + link          |
    | `email.bounced`     | Email could not be delivered (invalid address etc) | **Auto-unsubscribe**      |
    | `email.complained`  | Recipient marked email as spam                     | **Auto-unsubscribe**      |

    ---

    **Why bounces and complaints auto-unsubscribe:**
    Resend monitors your account's bounce and complaint rates. If they get
    too high, your sending ability gets restricted. Unsubscribing on bounce
    protects your sender reputation automatically.

    ---

    **Signature verification:**
    Every request from Resend includes three `svix-*` headers. The handler
    verifies the signature using `WEBHOOK_SECRET` from your `.env` file.

    - If `WEBHOOK_SECRET` is set → signature is verified on every request.
      Invalid signatures return `401 Unauthorized`.
    - If `WEBHOOK_SECRET` is not set → verification is skipped (useful for
      local development before you have a signing secret).

    ---

    **What Resend expects back:**
    Always return HTTP `200`. If Resend receives anything other than a 2xx
    response, it will **retry** the webhook delivery up to 5 times over 24 hours.
    This means your handler must be **idempotent** — processing the same event
    twice should not cause duplicate side effects.

    ---

    **What's NOT allowed:**
    - Do not return a non-2xx status unless you genuinely want Resend to retry.
    - Do not do heavy processing synchronously in this handler — it will slow
      down the response. Use a background task or queue for anything slow.
    """
    payload = await request.body()

    # ── Verify signature if secret is configured ──────────────────────────────
    secret = _get_secret()
    if secret:
        if not all([svix_id, svix_timestamp, svix_signature]):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Missing Svix signature headers (svix-id, svix-timestamp, svix-signature). "
                    "Make sure you are calling this endpoint from Resend, not directly."
                )
            )
        if not _verify_svix_signature(payload, svix_id, svix_timestamp, svix_signature, secret):
            raise HTTPException(
                status_code=401,
                detail="Webhook signature verification failed. Check your WEBHOOK_SECRET."
            )

    event = await request.json()
    event_type = event.get("type", "")
    data = event.get("data", {})

    # ── Route by event type ───────────────────────────────────────────────────

    now = datetime.utcnow()

    if event_type == "email.sent":
        email_id = data.get("email_id")
        if email_id:
            await db.execute(
                update(CampaignRecipient)
                .where(CampaignRecipient.resend_email_id == email_id)
                .values(status="sent", updated_at=now)
            )
            await db.commit()

    elif event_type == "email.delivered":
        email_id = data.get("email_id")
        if email_id:
            await db.execute(
                update(CampaignRecipient)
                .where(CampaignRecipient.resend_email_id == email_id)
                .values(status="delivered", updated_at=now)
            )
            await db.commit()

    elif event_type == "email.opened":
        email_id = data.get("email_id")
        logger.info(f"[WEBHOOK] email.opened → id={email_id}")

    elif event_type == "email.clicked":
        email_id = data.get("email_id")
        link = data.get("click", {}).get("link")
        logger.info(f"[WEBHOOK] email.clicked → id={email_id} link={link}")

    elif event_type == "email.bounced":
        email_id = data.get("email_id")
        if email_id:
            await db.execute(
                update(CampaignRecipient)
                .where(CampaignRecipient.resend_email_id == email_id)
                .values(status="bounced", updated_at=now)
            )
            # Derive contact_id from the CampaignRecipient row rather than
            # matching on raw email — the latter has no customer_id filter and
            # would unsubscribe the same address across all tenants.
            contact_id_subq = (
                select(CampaignRecipient.contact_id)
                .where(
                    CampaignRecipient.resend_email_id == email_id,
                    CampaignRecipient.contact_id.isnot(None),
                )
                .scalar_subquery()
            )
            await db.execute(
                update(Contact)
                .where(Contact.id == contact_id_subq)
                .values(is_subscribed=False)
            )
            await db.commit()

    elif event_type == "email.complained":
        email_id = data.get("email_id")
        if email_id:
            await db.execute(
                update(CampaignRecipient)
                .where(CampaignRecipient.resend_email_id == email_id)
                .values(status="complained", updated_at=now)
            )
            contact_id_subq = (
                select(CampaignRecipient.contact_id)
                .where(
                    CampaignRecipient.resend_email_id == email_id,
                    CampaignRecipient.contact_id.isnot(None),
                )
                .scalar_subquery()
            )
            await db.execute(
                update(Contact)
                .where(Contact.id == contact_id_subq)
                .values(is_subscribed=False)
            )
            await db.commit()

    else:
        logger.info(f"[WEBHOOK] Unhandled event type: {event_type} — ignoring")

    # Always return 200 — anything else causes Resend to retry
    return {"received": True, "type": event_type}
