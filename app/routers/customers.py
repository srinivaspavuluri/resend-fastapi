from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from typing import Optional

from ..database import get_db
from ..models import Customer
from ..services import resend_service

router = APIRouter(prefix="/customers", tags=["Customers & Domains"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CreateCustomerRequest(BaseModel):
    name: str = Field(
        ...,
        description="Display name for this customer (your client).",
        examples=["Acme Corp"]
    )


class UpdateCustomerRequest(BaseModel):
    name: Optional[str] = Field(
        None,
        description="New display name for this customer.",
        examples=["Acme Corp (Updated)"]
    )
    domain_name: Optional[str] = Field(
        None,
        description=(
            "Update the domain name stored in our DB. "
            "This does NOT register a new domain with Resend — "
            "use POST /customers/{id}/domains for that."
        ),
        examples=["newdomain.com"]
    )


class AddDomainRequest(BaseModel):
    domain_name: str = Field(
        ...,
        description=(
            "The domain this customer will send emails from. "
            "Must be a domain they own and can add DNS records to."
        ),
        examples=["acme.com"]
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/", summary="Create a new customer")
async def create_customer(
    body: CreateCustomerRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Creates a new customer record in the database.

    A **customer** represents one of your clients — each customer gets their
    own sending domain, their own contacts, and their own segments.

    **What you send:**
    ```json
    { "name": "Acme Corp" }
    ```

    **What you get back:**
    ```json
    { "id": "uuid-here", "name": "Acme Corp" }
    ```

    **Next step:** Add and verify a domain for this customer using
    `POST /customers/{id}/domains`.

    **Note:** The customer has no domain yet after creation. They cannot
    send emails until a domain is verified.
    """
    customer = Customer(name=body.name)
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return {"id": customer.id, "name": customer.name}


@router.get("/", summary="List all customers")
async def list_customers(db: AsyncSession = Depends(get_db)):
    """
    Returns all customers with their domain and verification status.

    **What you get back (array):**
    ```json
    [
      {
        "id": "uuid-here",
        "name": "Acme Corp",
        "domain": "acme.com",
        "domain_verified": true
      }
    ]
    ```

    - `domain_verified: false` means emails cannot be sent yet for that customer.
    - `domain: null` means no domain has been added yet.
    """
    result = await db.execute(select(Customer))
    customers = result.scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "domain": c.domain_name,
            "domain_verified": c.domain_verified,
        }
        for c in customers
    ]


@router.patch("/{customer_id}", summary="Update customer details")
async def update_customer(
    customer_id: str,
    body: UpdateCustomerRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Updates a customer's name or stored domain name.

    Only send the fields you want to change — any field left as `null`
    is ignored and the existing value is kept.

    **What you send (update name only):**
    ```json
    { "name": "Acme Corp Renamed" }
    ```

    **What you send (update both):**
    ```json
    { "name": "Acme Corp", "domain_name": "acme-new.com" }
    ```

    **What you get back:**
    ```json
    {
      "id": "customer-uuid",
      "name": "Acme Corp Renamed",
      "domain": "acme.com",
      "domain_verified": true
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if customer not found.
    - Updating `domain_name` here only changes the label in our database.
      To register and verify a new domain with Resend, you must still call
      `POST /customers/{id}/domains` — that resets `domain_verified` to false.
    - Passing all null fields (empty body) returns the customer unchanged —
      no error, no update.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    if body.name is not None:
        customer.name = body.name
    if body.domain_name is not None:
        customer.domain_name = body.domain_name

    await db.commit()
    await db.refresh(customer)

    return {
        "id": customer.id,
        "name": customer.name,
        "domain": customer.domain_name,
        "domain_verified": customer.domain_verified,
    }


@router.delete("/{customer_id}", summary="Delete a customer and all their data")
async def delete_customer(
    customer_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Permanently deletes a customer and **all associated data** —
    contacts, segments, and campaign history.

    **No request body required.**

    **What you get back:**
    ```json
    {
      "deleted": true,
      "customer_id": "customer-uuid",
      "message": "Customer and all associated data permanently deleted."
    }
    ```

    **What's NOT allowed:**
    - Returns `404` if the customer does not exist.

    **⚠️ This action is irreversible.**
    All contacts, segments, and campaign records for this customer
    are deleted from the database via CASCADE.

    **Note on Resend:** This does NOT remove the domain from Resend.
    If you want to remove the domain from your Resend account as well,
    do that manually in the Resend dashboard.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    await db.delete(customer)
    await db.commit()

    return {
        "deleted": True,
        "customer_id": customer_id,
        "message": "Customer and all associated data permanently deleted.",
    }


@router.post("/{customer_id}/domains", summary="Step 1 — Register customer domain with Resend")
async def add_domain(
    customer_id: str,
    body: AddDomainRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    **Step 1 of 2** for domain setup.

    Registers the customer's domain with Resend and returns the DNS records
    they need to add to their domain registrar (e.g. GoDaddy, Namecheap,
    Cloudflare, Route 53).

    **What you send:**
    ```json
    { "domain_name": "acme.com" }
    ```

    **What you get back:**
    ```json
    {
      "domain": "acme.com",
      "resend_domain_id": "dom_abc123",
      "status": "not_started",
      "dns_records": [
        { "type": "MX",  "name": "send", "value": "feedback-smtp.us-east-1.amazonses.com", "priority": 10 },
        { "type": "TXT", "name": "send", "value": "v=spf1 include:amazonses.com ~all" },
        { "type": "TXT", "name": "resend._domainkey", "value": "p=MIGfMA0..." }
      ],
      "next_step": "..."
    }
    ```

    **What happens next:**
    1. Share the `dns_records` with your customer.
    2. They add those records in their domain registrar's DNS settings.
    3. Once added, call `POST /customers/{id}/domains/verify`.

    **What's NOT allowed:**
    - You cannot send emails from this domain until verification is complete.
    - The domain must be a real domain your customer controls — you cannot
      verify a domain you don't own DNS records for.
    - Calling this endpoint again for the same customer will overwrite the
      previous domain entry.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    try:
        resend_resp = resend_service.add_domain(body.domain_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Resend error: {str(e)}")

    customer.domain_name = body.domain_name
    customer.resend_domain_id = resend_resp.get("id")
    customer.domain_verified = False
    await db.commit()

    return {
        "domain": body.domain_name,
        "resend_domain_id": resend_resp.get("id"),
        "status": resend_resp.get("status"),
        "dns_records": resend_resp.get("records", []),
        "next_step": (
            "Share the dns_records above with your customer. "
            "Once they add them to their domain registrar, call "
            f"POST /customers/{customer_id}/domains/verify"
        ),
    }


@router.post("/{customer_id}/domains/verify", summary="Step 2 — Trigger domain verification")
async def verify_domain(
    customer_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    **Step 2 of 2** for domain setup.

    Asks Resend to check whether the customer has correctly added their
    DNS records. If they have, the domain is marked as verified and
    the customer can start sending emails.

    **No request body required.**

    **What you get back (verified):**
    ```json
    {
      "domain": "acme.com",
      "verified": true,
      "status": "verified",
      "message": "Domain verified. This customer can now send emails."
    }
    ```

    **What you get back (not yet verified):**
    ```json
    {
      "domain": "acme.com",
      "verified": false,
      "status": "pending",
      "message": "Not verified yet. DNS changes can take up to 48 hours. Try again later."
    }
    ```

    **Important — DNS propagation delay:**
    DNS changes are not instant. After your customer adds the records,
    it can take anywhere from a few minutes to 48 hours for the changes
    to propagate globally. If this returns `verified: false`, wait and
    try again. Use `GET /customers/{id}/domains/status` to poll without
    re-triggering.

    **What's NOT allowed:**
    - Cannot call this before `POST /customers/{id}/domains` has been called.
    - Will return an error if no domain is registered for this customer.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not customer.resend_domain_id:
        raise HTTPException(
            status_code=400,
            detail="No domain registered for this customer. Call POST /customers/{id}/domains first."
        )

    try:
        resend_service.verify_domain(customer.resend_domain_id)
        status_resp = resend_service.get_domain_status(customer.resend_domain_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Resend error: {str(e)}")

    is_verified = status_resp.get("status") == "verified"

    if is_verified:
        customer.domain_verified = True
        await db.commit()

    return {
        "domain": customer.domain_name,
        "verified": is_verified,
        "status": status_resp.get("status"),
        "message": (
            "Domain verified. This customer can now send emails."
            if is_verified
            else "Not verified yet. DNS changes can take up to 48 hours. Try again later."
        ),
    }


@router.get("/{customer_id}/domains/status", summary="Poll domain verification status")
async def domain_status(
    customer_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Checks the current DNS verification status without re-triggering
    the verification process.

    Use this endpoint to **poll** from your frontend every 30–60 seconds
    while waiting for your customer to add their DNS records.

    **What you get back:**
    ```json
    {
      "domain": "acme.com",
      "verified": false,
      "status": "pending"
    }
    ```

    **Possible status values from Resend:**
    - `not_started` — DNS records have not been checked yet
    - `pending`     — Records added but propagation not confirmed yet
    - `verified`    — Domain is ready for sending ✅
    - `failed`      — Verification failed (records may be wrong or missing)

    **What's NOT allowed:**
    - Returns 404 if no domain has been added for this customer yet.
    """
    result = await db.execute(select(Customer).where(Customer.id == customer_id))
    customer = result.scalar_one_or_none()
    if not customer or not customer.resend_domain_id:
        raise HTTPException(status_code=404, detail="No domain found for this customer")

    try:
        status_resp = resend_service.get_domain_status(customer.resend_domain_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Resend error: {str(e)}")

    is_verified = status_resp.get("status") == "verified"
    if is_verified and not customer.domain_verified:
        customer.domain_verified = True
        await db.commit()

    return {
        "domain": customer.domain_name,
        "verified": is_verified,
        "status": status_resp.get("status"),
    }
