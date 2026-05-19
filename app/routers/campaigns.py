from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from ..database import get_db
from ..models import Campaign

router = APIRouter(prefix="/customers", tags=["Campaigns"])


@router.get("/{customer_id}/campaigns", summary="List all campaigns sent by a customer")
async def list_campaigns(
    customer_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns the full history of campaigns sent for a customer —
    every time `POST /customers/{id}/send` was called.

    **What you get back:**
    ```json
    [
      {
        "id": "campaign-uuid",
        "subject": "Hello {{first_name}}, big news this month!",
        "sent_to_count": 47,
        "from_address": "hello@acme.com",
        "targeting": { "tag": "premium" },
        "status": "sent",
        "sent_at": "2026-04-18T10:30:00"
      }
    ]
    ```

    **About `targeting`:**
    Shows exactly how the campaign was targeted:
    - `{}` — sent to all contacts
    - `{"tag": "premium"}` — sent to contacts tagged "premium"
    - `{"segment_id": "uuid"}` — sent to a specific segment

    **What's NOT allowed:**
    - Returns an empty list (not an error) if no campaigns exist yet.
    - You cannot see campaigns belonging to a different customer.
    """
    result = await db.execute(
        select(Campaign)
        .where(Campaign.customer_id == customer_id)
        .order_by(Campaign.sent_at.desc())
    )
    campaigns = result.scalars().all()

    return [
        {
            "id": c.id,
            "subject": c.subject,
            "sent_to_count": c.sent_to_count,
            "from_address": c.from_address,
            "targeting": c.targeting,
            "status": c.status,
            "sent_at": c.sent_at,
        }
        for c in campaigns
    ]


@router.get("/{customer_id}/campaigns/{campaign_id}", summary="Get a single campaign")
async def get_campaign(
    customer_id: str,
    campaign_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns details of a specific campaign.

    **What you get back:**
    ```json
    {
      "id": "campaign-uuid",
      "subject": "Hello {{first_name}}, big news this month!",
      "sent_to_count": 47,
      "from_address": "hello@acme.com",
      "targeting": {},
      "status": "sent",
      "sent_at": "2026-04-18T10:30:00"
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if campaign not found or belongs to a different customer.
    """
    result = await db.execute(
        select(Campaign).where(
            and_(Campaign.id == campaign_id, Campaign.customer_id == customer_id)
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(
            status_code=404,
            detail="Campaign not found or does not belong to this customer"
        )

    return {
        "id": campaign.id,
        "subject": campaign.subject,
        "sent_to_count": campaign.sent_to_count,
        "from_address": campaign.from_address,
        "targeting": campaign.targeting,
        "status": campaign.status,
        "sent_at": campaign.sent_at,
    }


@router.delete("/{customer_id}/campaigns/{campaign_id}", summary="Delete a campaign record")
async def delete_campaign(
    customer_id: str,
    campaign_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Deletes a campaign record from your database.

    **No request body required.**

    **What you get back:**
    ```json
    {
      "deleted": true,
      "campaign_id": "campaign-uuid",
      "message": "Campaign record deleted. Emails already sent are not affected."
    }
    ```

    **Important — what this does and does NOT do:**
    - ✅ Removes the campaign from your campaign history in this database.
    - ❌ Does NOT unsend any emails — emails already delivered cannot be recalled.
    - ❌ Does NOT remove anything from Resend's dashboard.

    Use this to clean up test campaigns or remove records you no longer need.

    **What's NOT allowed:**
    - Returns `404` if campaign not found or belongs to a different customer.
    - You cannot edit a campaign's content — campaigns are immutable records
      of what was sent. If you need to resend with different content,
      call `POST /customers/{id}/send` again.
    """
    result = await db.execute(
        select(Campaign).where(
            and_(Campaign.id == campaign_id, Campaign.customer_id == customer_id)
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(
            status_code=404,
            detail="Campaign not found or does not belong to this customer"
        )

    await db.delete(campaign)
    await db.commit()

    return {
        "deleted": True,
        "campaign_id": campaign_id,
        "message": "Campaign record deleted. Emails already sent are not affected.",
    }
