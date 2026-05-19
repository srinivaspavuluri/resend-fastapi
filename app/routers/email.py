from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from ..database import get_db
from ..models import Customer, Contact, Segment, ContactSegment, Campaign
from ..services.resend_service import send_single, send_bulk, EmailRecipient

router = APIRouter(prefix="/customers", tags=["Send Email"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class SendEmailRequest(BaseModel):
    subject: str = Field(
        ...,
        max_length=998,    # RFC 5322 hard limit for email subject lines
        description=(
            "Email subject line. Supports {{first_name}} personalisation — "
            "it will be replaced with each contact's first name when sending."
        ),
        examples=["Hello {{first_name}}, here's your monthly update"]
    )
    html: str = Field(
        ...,
        max_length=500_000,    # 500 KB — generous for real emails, guards against huge payloads
        description=(
            "Full HTML body of the email. Supports {{first_name}} personalisation. "
            "Use standard HTML — inline styles are recommended for email clients."
        ),
        examples=["<h1>Hi {{first_name}}!</h1><p>Here is your update for this month.</p>"]
    )
    segment_id: Optional[str] = Field(
        None,
        description=(
            "Send only to contacts in this segment. "
            "Get segment IDs from POST /customers/{id}/segments. "
            "Cannot be used together with `tag` — if both are provided, "
            "`segment_id` takes priority and `tag` is ignored."
        ),
        examples=["segment-uuid-here"]
    )
    tag: Optional[str] = Field(
        None,
        description=(
            "Send only to contacts that have this tag. "
            "Tags are set when adding contacts via POST /customers/{id}/contacts. "
            "Example: pass 'premium' to send only to contacts tagged 'premium'."
        ),
        examples=["premium"]
    )


class SendSingleRequest(BaseModel):
    to_email: EmailStr = Field(
        ...,
        description=(
            "Recipient email address. Does not need to be in your contacts list — "
            "use this for one-off transactional emails like confirmations or alerts."
        ),
        examples=["john.doe@example.com"]
    )
    subject: str = Field(
        ...,
        max_length=998,
        description="Subject line for this email.",
        examples=["Your order has been confirmed"]
    )
    html: str = Field(
        ...,
        max_length=500_000,
        description="Full HTML content for this email.",
        examples=["<p>Thank you for your order! It will arrive in 3–5 days.</p>"]
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{customer_id}/send", summary="Send a campaign to customer's contacts")
async def send_email(
    customer_id: str,
    body: SendEmailRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Sends an email to a customer's contacts through Resend.
    Emails go out from `hello@{customer's verified domain}`.

    ---

    **Targeting — who receives the email:**

    | What you pass         | Who gets the email                          |
    |-----------------------|---------------------------------------------|
    | Nothing (default)     | All subscribed contacts for this customer   |
    | `segment_id`          | Only contacts in that segment               |
    | `tag`                 | Only contacts with that tag                 |
    | Both `segment_id` + `tag` | `segment_id` wins, `tag` is ignored     |

    ---

    **Personalisation:**
    Use `{{first_name}}` anywhere in `subject` or `html` — it gets replaced
    with each recipient's first name. If a contact has no first name,
    it falls back to `"there"`.

    ---

    **Example request (send to all):**
    ```json
    {
      "subject": "Hello {{first_name}}, big news this month!",
      "html": "<h1>Hi {{first_name}}!</h1><p>Here's what's new...</p>"
    }
    ```

    **Example request (send to a tag):**
    ```json
    {
      "subject": "Exclusive offer for Premium members",
      "html": "<p>Hi {{first_name}}, as a premium member you get...</p>",
      "tag": "premium"
    }
    ```

    **What you get back:**
    ```json
    {
      "success": true,
      "sent_to": 47,
      "from": "hello@acme.com",
      "batches_used": 1
    }
    ```

    `batches_used` tells you how many Resend API calls were made.
    Resend's batch limit is 100 emails per call, so 250 contacts = 3 batches.

    ---

    **What's NOT allowed:**
    - Customer's domain must be verified. Returns `400` if not.
    - Returns `400` if no subscribed contacts match the targeting criteria.
    - Unsubscribed contacts are always excluded — there is no override.
    - Returns `502` if Resend's API fails (e.g. rate limit or network error).
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()

    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not customer.domain_verified:
        raise HTTPException(
            status_code=400,
            detail=(
                "This customer's domain is not verified. "
                "Complete setup: POST /customers/{id}/domains → "
                "POST /customers/{id}/domains/verify"
            ),
        )

    # ── Fetch target contacts ─────────────────────────────────────────────────
    if body.segment_id:
        seg_result = await db.execute(
            select(Segment).where(
                and_(Segment.id == body.segment_id, Segment.customer_id == customer_id)
            )
        )
        if not seg_result.scalar_one_or_none():
            raise HTTPException(
                status_code=404,
                detail="Segment not found or does not belong to this customer"
            )
        query = (
            select(Contact)
            .join(ContactSegment, Contact.id == ContactSegment.contact_id)
            .where(
                and_(
                    ContactSegment.segment_id == body.segment_id,
                    Contact.customer_id == customer_id,
                    Contact.is_subscribed == True,
                )
            )
        )
    else:
        query = select(Contact).where(
            and_(
                Contact.customer_id == customer_id,
                Contact.is_subscribed == True,
            )
        )

    contacts_result = await db.execute(query)
    contacts = contacts_result.scalars().all()

    if body.tag and not body.segment_id:
        contacts = [c for c in contacts if c.tags and body.tag in c.tags]

    if not contacts:
        raise HTTPException(
            status_code=400,
            detail=(
                "No subscribed contacts found for the given targeting criteria. "
                "Check that the customer has contacts and that they are subscribed."
            )
        )

    recipients = [
        EmailRecipient(email=c.email, first_name=c.first_name or "")
        for c in contacts
    ]

    try:
        results = send_bulk(
            from_domain=customer.domain_name,
            recipients=recipients,
            subject=body.subject,
            html_template=body.html,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Resend delivery error: {str(e)}")

    # ── Save campaign record ──────────────────────────────────────────────────
    targeting = {}
    if body.segment_id:
        targeting = {"segment_id": body.segment_id}
    elif body.tag:
        targeting = {"tag": body.tag}

    campaign = Campaign(
        customer_id=customer_id,
        subject=body.subject,
        sent_to_count=len(recipients),
        from_address=f"hello@{customer.domain_name}",
        targeting=targeting,
        status="sent",
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    return {
        "success": True,
        "campaign_id": campaign.id,
        "sent_to": len(recipients),
        "from": f"hello@{customer.domain_name}",
        "batches_used": len(results),
    }


@router.post("/{customer_id}/send/single", summary="Send a single transactional email")
async def send_single_email(
    customer_id: str,
    body: SendSingleRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Sends one email to a specific address. The recipient does not need
    to be in your contacts list.

    Use this for transactional emails — things that are triggered by an
    action, not a campaign:
    - Order confirmations
    - Password reset links
    - Booking notifications
    - One-off alerts

    **What you send:**
    ```json
    {
      "to_email": "john.doe@example.com",
      "subject": "Your order #1234 has shipped",
      "html": "<p>Great news! Your order is on its way.</p>"
    }
    ```

    **What you get back:**
    ```json
    {
      "success": true,
      "email_id": "resend-email-uuid",
      "to": "john.doe@example.com",
      "from": "hello@acme.com"
    }
    ```

    Save the `email_id` if you want to track delivery status via Resend's
    dashboard or by checking incoming webhook events.

    **What's NOT allowed:**
    - Domain must be verified. Returns `400` if not.
    - Invalid email format is rejected by validation before hitting Resend.
    - Returns `502` if Resend's API is unreachable or returns an error.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()

    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not customer.domain_verified:
        raise HTTPException(status_code=400, detail="Domain not verified for this customer")

    try:
        resend_result = send_single(
            from_domain=customer.domain_name,
            to_email=body.to_email,
            subject=body.subject,
            html=body.html,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Resend error: {str(e)}")

    return {
        "success": True,
        "email_id": resend_result.get("id"),
        "to": body.to_email,
        "from": f"hello@{customer.domain_name}",
    }
