from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional

from ..database import get_db
from ..models import Contact, Customer, Segment, ContactSegment

router = APIRouter(prefix="/customers", tags=["Contacts & Segments"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class AddContactRequest(BaseModel):
    email: EmailStr = Field(
        ...,
        description="Valid email address for this contact.",
        examples=["john.doe@example.com"]
    )
    first_name: Optional[str] = Field(
        None,
        description="First name — used for {{first_name}} personalisation in emails.",
        examples=["John"]
    )
    last_name: Optional[str] = Field(
        None,
        description="Last name (optional).",
        examples=["Doe"]
    )
    tags: Optional[List[str]] = Field(
        default=[],
        description=(
            "List of tags to group this contact. Use tags to target specific "
            "groups when sending — e.g. ['newsletter', 'premium']."
        ),
        examples=[["newsletter", "premium"]]
    )


class UpdateContactRequest(BaseModel):
    email: Optional[EmailStr] = Field(
        None,
        description="New email address. Must be unique within this customer.",
        examples=["john.new@example.com"]
    )
    first_name: Optional[str] = Field(None, examples=["John"])
    last_name: Optional[str] = Field(None, examples=["Doe"])
    tags: Optional[List[str]] = Field(
        None,
        description=(
            "Replaces the entire tags list. To add a tag, include all existing "
            "tags plus the new one. To remove a tag, omit it from the list."
        ),
        examples=[["newsletter", "vip"]]
    )
    is_subscribed: Optional[bool] = Field(
        None,
        description="Set to true to re-subscribe a previously unsubscribed contact.",
        examples=[True]
    )


class UpdateSegmentRequest(BaseModel):
    name: str = Field(
        ...,
        description="New name for this segment.",
        examples=["VIP Customers"]
    )


class CreateSegmentRequest(BaseModel):
    name: str = Field(
        ...,
        description="A label for this group of contacts.",
        examples=["Premium Users"]
    )


class AddContactsToSegmentRequest(BaseModel):
    contact_ids: List[str] = Field(
        ...,
        description=(
            "List of contact IDs (from POST /contacts) to add to this segment. "
            "IDs that don't belong to this customer are silently skipped. "
            "Duplicate additions are also silently skipped."
        ),
        examples=[["contact-uuid-1", "contact-uuid-2"]]
    )


# ── Contact routes ────────────────────────────────────────────────────────────

@router.post("/{customer_id}/contacts", summary="Add a contact for a customer")
async def add_contact(
    customer_id: str,
    body: AddContactRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Adds a contact to a customer's list. Contacts are stored entirely
    in your own database — nothing is sent to Resend at this point.
    Resend only gets involved when you actually send an email.

    **What you send:**
    ```json
    {
      "email": "john.doe@example.com",
      "first_name": "John",
      "last_name": "Doe",
      "tags": ["newsletter", "premium"]
    }
    ```

    **What you get back:**
    ```json
    {
      "id": "contact-uuid",
      "email": "john.doe@example.com",
      "first_name": "John",
      "tags": ["newsletter", "premium"]
    }
    ```

    **About tags:**
    Tags are free-form strings you define. Use them to target contacts
    when sending — e.g. send only to contacts tagged `"premium"` by passing
    `tag: "premium"` in the send endpoint.

    **About `{{first_name}}` personalisation:**
    If `first_name` is provided, it will replace `{{first_name}}` in your
    email subject and body when sending. If not provided, it falls back to
    `"there"` (e.g. "Hello there!").

    **What's NOT allowed:**
    - The same email address cannot be added twice for the same customer.
      Returns `409 Conflict` if a duplicate is detected.
    - The customer must exist. Returns `404` if not found.
    - Invalid email formats are rejected automatically.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Customer not found")

    existing = await db.execute(
        select(Contact).where(
            and_(Contact.customer_id == customer_id, Contact.email == body.email)
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"A contact with email '{body.email}' already exists for this customer."
        )

    contact = Contact(
        customer_id=customer_id,
        email=body.email,
        first_name=body.first_name,
        last_name=body.last_name,
        tags=body.tags or [],
    )
    db.add(contact)
    await db.commit()
    await db.refresh(contact)

    return {
        "id": contact.id,
        "email": contact.email,
        "first_name": contact.first_name,
        "tags": contact.tags,
    }


@router.get("/{customer_id}/contacts", summary="List contacts for a customer")
async def list_contacts(
    customer_id: str,
    tag: Optional[str] = None,
    subscribed_only: bool = True,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns contacts belonging to a customer.

    **Query parameters:**

    - `tag` *(optional)*: Filter by a specific tag.
      Example: `?tag=premium` returns only contacts tagged `"premium"`.

    - `subscribed_only` *(default: true)*: When `true`, only returns contacts
      where `is_subscribed = true`. Set to `false` to include unsubscribed
      contacts (e.g. for auditing).

    **Example response:**
    ```json
    [
      {
        "id": "contact-uuid",
        "email": "john.doe@example.com",
        "first_name": "John",
        "last_name": "Doe",
        "tags": ["newsletter", "premium"],
        "is_subscribed": true
      }
    ]
    ```

    **What's NOT allowed:**
    - You cannot see contacts from a different customer — each customer's
      data is fully isolated by `customer_id`.
    """
    query = select(Contact).where(Contact.customer_id == customer_id)

    if subscribed_only:
        query = query.where(Contact.is_subscribed == True)

    result = await db.execute(query)
    contacts = result.scalars().all()

    if tag:
        contacts = [c for c in contacts if c.tags and tag in c.tags]

    return [
        {
            "id": c.id,
            "email": c.email,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "tags": c.tags,
            "is_subscribed": c.is_subscribed,
        }
        for c in contacts
    ]


@router.patch("/{customer_id}/contacts/{contact_id}", summary="Update a contact")
async def update_contact(
    customer_id: str,
    contact_id: str,
    body: UpdateContactRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Updates any field on a contact. Only the fields you send are changed —
    fields left as `null` keep their existing values.

    **What you send (update tags only):**
    ```json
    { "tags": ["newsletter", "vip"] }
    ```

    **What you send (update email and name):**
    ```json
    {
      "email": "john.new@example.com",
      "first_name": "Johnny"
    }
    ```

    **What you send (re-subscribe a contact):**
    ```json
    { "is_subscribed": true }
    ```

    **What you get back:**
    ```json
    {
      "id": "contact-uuid",
      "email": "john.new@example.com",
      "first_name": "Johnny",
      "last_name": "Doe",
      "tags": ["newsletter", "vip"],
      "is_subscribed": true
    }
    ```

    **What's NOT allowed:**
    - Cannot change the email to one already used by another contact
      under the same customer. Returns `409 Conflict`.
    - Returns `404` if contact not found or belongs to a different customer.
    - Sending an empty body (all nulls) returns the contact unchanged — no error.
    """
    result = await db.execute(
        select(Contact).where(
            and_(Contact.id == contact_id, Contact.customer_id == customer_id)
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found for this customer")

    if body.email is not None and body.email != contact.email:
        dup = await db.execute(
            select(Contact).where(
                and_(Contact.customer_id == customer_id, Contact.email == body.email)
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Another contact with email '{body.email}' already exists."
            )
        contact.email = body.email

    if body.first_name is not None:
        contact.first_name = body.first_name
    if body.last_name is not None:
        contact.last_name = body.last_name
    if body.tags is not None:
        contact.tags = body.tags
    if body.is_subscribed is not None:
        contact.is_subscribed = body.is_subscribed

    await db.commit()
    await db.refresh(contact)

    return {
        "id": contact.id,
        "email": contact.email,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "tags": contact.tags,
        "is_subscribed": contact.is_subscribed,
    }


@router.delete("/{customer_id}/contacts/{contact_id}", summary="Delete a contact")
async def delete_contact(
    customer_id: str,
    contact_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Permanently deletes a contact from the database.

    **No request body required.**

    **What you get back:**
    ```json
    {
      "deleted": true,
      "contact_id": "contact-uuid",
      "email": "john.doe@example.com"
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if contact not found or belongs to a different customer.

    **⚠️ This is irreversible.**
    The contact is removed from all segments they belonged to as well
    (CASCADE delete on contact_segments).

    **Tip:** If a contact just wants to stop receiving emails, prefer
    `PATCH /contacts/{id}/unsubscribe` over deleting — it keeps the record
    for auditing while stopping future sends.
    """
    result = await db.execute(
        select(Contact).where(
            and_(Contact.id == contact_id, Contact.customer_id == customer_id)
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found for this customer")

    email = contact.email
    await db.delete(contact)
    await db.commit()

    return {"deleted": True, "contact_id": contact_id, "email": email}


@router.patch(
    "/{customer_id}/contacts/{contact_id}/unsubscribe",
    summary="Unsubscribe a contact"
)
async def unsubscribe_contact(
    customer_id: str,
    contact_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Marks a contact as unsubscribed. Unsubscribed contacts are automatically
    excluded from all future email sends.

    **No request body required.**

    **What you get back:**
    ```json
    { "email": "john.doe@example.com", "is_subscribed": false }
    ```

    **When to use this:**
    - When a contact clicks "unsubscribe" in your app or email footer.
    - This is also called automatically by the webhook handler when Resend
      reports a hard bounce or spam complaint.

    **What's NOT allowed:**
    - Cannot unsubscribe a contact that belongs to a different customer.
    - Returns `404` if the contact ID is not found under this customer.

    **Note:** This action sets `is_subscribed = false` in your database.
    It does NOT call Resend — Resend has no concept of subscriptions.
    """
    result = await db.execute(
        select(Contact).where(
            and_(Contact.id == contact_id, Contact.customer_id == customer_id)
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found for this customer")

    contact.is_subscribed = False
    await db.commit()
    return {"email": contact.email, "is_subscribed": False}


# ── Segment routes ────────────────────────────────────────────────────────────

@router.get("/{customer_id}/segments", summary="List all segments for a customer")
async def list_segments(
    customer_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns all segments belonging to a customer, with a count of
    how many contacts are in each.

    **What you get back:**
    ```json
    [
      {
        "id": "segment-uuid",
        "name": "Premium Users",
        "contact_count": 12,
        "created_at": "2026-04-01T10:00:00"
      }
    ]
    ```

    **What's NOT allowed:**
    - Returns an empty list (not an error) if no segments exist yet.
    """
    result = await db.execute(
        select(Segment).where(Segment.customer_id == customer_id)
    )
    segments = result.scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "contact_count": len(s.contacts),
            "created_at": s.created_at,
        }
        for s in segments
    ]


@router.get("/{customer_id}/segments/{segment_id}", summary="Get a segment and its contacts")
async def get_segment(
    customer_id: str,
    segment_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns a segment with its full list of contacts.

    **What you get back:**
    ```json
    {
      "id": "segment-uuid",
      "name": "Premium Users",
      "contacts": [
        { "id": "contact-uuid", "email": "john@example.com", "first_name": "John" }
      ]
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if segment not found or belongs to a different customer.
    """
    result = await db.execute(
        select(Segment).where(
            and_(Segment.id == segment_id, Segment.customer_id == customer_id)
        )
    )
    segment = result.scalar_one_or_none()
    if not segment:
        raise HTTPException(
            status_code=404,
            detail="Segment not found or does not belong to this customer"
        )
    return {
        "id": segment.id,
        "name": segment.name,
        "contacts": [
            {"id": c.id, "email": c.email, "first_name": c.first_name}
            for c in segment.contacts
        ],
    }


@router.post("/{customer_id}/segments", summary="Create a segment for a customer")
async def create_segment(
    customer_id: str,
    body: CreateSegmentRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Creates a named segment (a group) that you can add contacts to
    and then target when sending emails.

    **What you send:**
    ```json
    { "name": "Premium Users" }
    ```

    **What you get back:**
    ```json
    {
      "id": "segment-uuid",
      "name": "Premium Users",
      "customer_id": "customer-uuid"
    }
    ```

    **What's a segment vs a tag?**
    - **Tags** are labels set directly on a contact. Simple and fast.
    - **Segments** are explicit groups you manage — you decide which
      contacts are in them via `POST /segments/{id}/contacts`.

    Use tags for broad categorisation. Use segments when you need
    precise, managed lists (e.g. "users who signed up in April 2026").

    **What's NOT allowed:**
    - Returns `404` if the customer does not exist.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Customer not found")

    segment = Segment(customer_id=customer_id, name=body.name)
    db.add(segment)
    await db.commit()
    await db.refresh(segment)

    return {"id": segment.id, "name": segment.name, "customer_id": customer_id}


@router.patch("/{customer_id}/segments/{segment_id}", summary="Rename a segment")
async def update_segment(
    customer_id: str,
    segment_id: str,
    body: UpdateSegmentRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Renames a segment. The contacts inside the segment are not affected.

    **What you send:**
    ```json
    { "name": "VIP Customers" }
    ```

    **What you get back:**
    ```json
    {
      "id": "segment-uuid",
      "name": "VIP Customers",
      "customer_id": "customer-uuid"
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if segment not found or belongs to a different customer.
    """
    result = await db.execute(
        select(Segment).where(
            and_(Segment.id == segment_id, Segment.customer_id == customer_id)
        )
    )
    segment = result.scalar_one_or_none()
    if not segment:
        raise HTTPException(
            status_code=404,
            detail="Segment not found or does not belong to this customer"
        )

    segment.name = body.name
    await db.commit()
    await db.refresh(segment)

    return {"id": segment.id, "name": segment.name, "customer_id": customer_id}


@router.delete("/{customer_id}/segments/{segment_id}", summary="Delete a segment")
async def delete_segment(
    customer_id: str,
    segment_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Permanently deletes a segment. The contacts that were in the segment
    are **not** deleted — only the segment grouping is removed.

    **No request body required.**

    **What you get back:**
    ```json
    {
      "deleted": true,
      "segment_id": "segment-uuid",
      "message": "Segment deleted. Contacts were not affected."
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if segment not found or belongs to a different customer.

    **⚠️ This is irreversible.** You will need to recreate the segment
    and re-add contacts if deleted by mistake.
    """
    result = await db.execute(
        select(Segment).where(
            and_(Segment.id == segment_id, Segment.customer_id == customer_id)
        )
    )
    segment = result.scalar_one_or_none()
    if not segment:
        raise HTTPException(
            status_code=404,
            detail="Segment not found or does not belong to this customer"
        )

    await db.delete(segment)
    await db.commit()

    return {
        "deleted": True,
        "segment_id": segment_id,
        "message": "Segment deleted. Contacts were not affected.",
    }


@router.delete(
    "/{customer_id}/segments/{segment_id}/contacts/{contact_id}",
    summary="Remove a contact from a segment"
)
async def remove_contact_from_segment(
    customer_id: str,
    segment_id: str,
    contact_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Removes a single contact from a segment without deleting the contact.

    **No request body required.**

    **What you get back:**
    ```json
    {
      "removed": true,
      "contact_id": "contact-uuid",
      "segment_id": "segment-uuid"
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if the segment does not belong to this customer.
    - Returns `404` if the contact is not currently in this segment.
    - The contact itself is not deleted — only the segment membership is removed.
    """
    # Verify segment belongs to this customer
    seg_result = await db.execute(
        select(Segment).where(
            and_(Segment.id == segment_id, Segment.customer_id == customer_id)
        )
    )
    if not seg_result.scalar_one_or_none():
        raise HTTPException(
            status_code=404,
            detail="Segment not found or does not belong to this customer"
        )

    link_result = await db.execute(
        select(ContactSegment).where(
            and_(
                ContactSegment.contact_id == contact_id,
                ContactSegment.segment_id == segment_id
            )
        )
    )
    link = link_result.scalar_one_or_none()
    if not link:
        raise HTTPException(
            status_code=404,
            detail="This contact is not in the specified segment"
        )

    await db.delete(link)
    await db.commit()

    return {"removed": True, "contact_id": contact_id, "segment_id": segment_id}


@router.post(
    "/{customer_id}/segments/{segment_id}/contacts",
    summary="Add contacts to a segment"
)
async def add_contacts_to_segment(
    customer_id: str,
    segment_id: str,
    body: AddContactsToSegmentRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Adds one or more contacts to a segment.

    **What you send:**
    ```json
    {
      "contact_ids": ["contact-uuid-1", "contact-uuid-2", "contact-uuid-3"]
    }
    ```

    **What you get back:**
    ```json
    {
      "segment_id": "segment-uuid",
      "added_count": 2,
      "added": ["contact-uuid-1", "contact-uuid-2"]
    }
    ```

    **What's silently skipped (no error):**
    - Contact IDs that don't belong to this customer are ignored.
    - Contacts already in this segment are not added again (no duplicates).

    **What's NOT allowed:**
    - The segment must belong to this customer — returns `404` if not found.
    - You cannot add contacts from a different customer into this segment.

    **After adding contacts:** Use `POST /customers/{id}/send` with
    `segment_id` to send an email only to this segment.
    """
    result = await db.execute(
        select(Segment).where(
            and_(Segment.id == segment_id, Segment.customer_id == customer_id)
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=404,
            detail="Segment not found or does not belong to this customer"
        )

    added = []
    for contact_id in body.contact_ids:
        c_result = await db.execute(
            select(Contact).where(
                and_(Contact.id == contact_id, Contact.customer_id == customer_id)
            )
        )
        if not c_result.scalar_one_or_none():
            continue

        existing = await db.execute(
            select(ContactSegment).where(
                and_(
                    ContactSegment.contact_id == contact_id,
                    ContactSegment.segment_id == segment_id
                )
            )
        )
        if existing.scalar_one_or_none():
            continue

        db.add(ContactSegment(contact_id=contact_id, segment_id=segment_id))
        added.append(contact_id)

    await db.commit()
    return {"segment_id": segment_id, "added_count": len(added), "added": added}
