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
import hmac
import hashlib
import base64
import logging
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional

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

    if event_type == "email.sent":
        email_id = data.get("email_id")
        to = data.get("to", [])
        print(f"[WEBHOOK] email.sent → id={email_id} to={to}")
        # TODO: update EmailLog table: status = 'sent'

    elif event_type == "email.delivered":
        email_id = data.get("email_id")
        print(f"[WEBHOOK] email.delivered → id={email_id}")
        # TODO: update EmailLog table: status = 'delivered'

    elif event_type == "email.opened":
        email_id = data.get("email_id")
        print(f"[WEBHOOK] email.opened → id={email_id}")
        # TODO: log open event for analytics

    elif event_type == "email.clicked":
        email_id = data.get("email_id")
        link = data.get("click", {}).get("link")
        print(f"[WEBHOOK] email.clicked → id={email_id} link={link}")
        # TODO: log click event and which link was clicked

    elif event_type == "email.bounced":
        # Hard bounce — this address is invalid or unreachable
        # Unsubscribe to protect sender reputation
        to = data.get("to", [])
        print(f"[WEBHOOK] email.bounced → {to} — marking as unsubscribed")
        # TODO: set Contact.is_subscribed = False for each email in `to`

    elif event_type == "email.complained":
        # Spam complaint — unsubscribe immediately, no exceptions
        to = data.get("to", [])
        print(f"[WEBHOOK] email.complained → {to} — unsubscribing immediately")
        # TODO: set Contact.is_subscribed = False for each email in `to`

    else:
        print(f"[WEBHOOK] Unhandled event type: {event_type} — ignoring")

    # Always return 200 — anything else causes Resend to retry
    return {"received": True, "type": event_type}
