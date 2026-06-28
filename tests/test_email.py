"""
Tests for email send endpoints.

Endpoints covered:
  POST /customers/{id}/send          — bulk send (all / tag / segment targeting)
  POST /customers/{id}/send/single   — single transactional email

All Resend API calls (send_batch, send_single) are mocked in conftest.py.
A "verified customer" helper pre-wires domain setup so the send gate passes.
"""
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient
from sqlalchemy import select
from app.models import Campaign, CampaignRecipient
from tests.conftest import create_customer, create_verified_customer, add_contact


# ── POST /customers/{id}/send  (bulk) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_to_all_contacts(client: AsyncClient):
    info = await create_verified_customer(client)
    await add_contact(client, info["id"], email="a@example.com", first_name="Alice")
    await add_contact(client, info["id"], email="b@example.com", first_name="Bob")

    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "Hello {{first_name}}!",
        "html": "<p>Hi {{first_name}}, welcome!</p>",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["sent_to"] == 2
    assert data["from"] == "hello@acme.com"
    assert "campaign_id" in data


@pytest.mark.asyncio
async def test_send_creates_campaign_record(client: AsyncClient):
    """A Campaign row must be saved after a successful send."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])

    send_r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "Test",
        "html": "<p>Test</p>",
    })
    campaign_id = send_r.json()["campaign_id"]

    r = await client.get(f"/customers/{info['id']}/campaigns/{campaign_id}")
    assert r.status_code == 200
    assert r.json()["subject"] == "Test"
    assert r.json()["sent_to_count"] == 1


@pytest.mark.asyncio
async def test_send_domain_not_verified(client: AsyncClient):
    """Cannot send if domain is not verified — expect 400."""
    c = await create_customer(client)
    await add_contact(client, c["id"])
    r = await client.post(f"/customers/{c['id']}/send", json={
        "subject": "Hi",
        "html": "<p>Hi</p>",
    })
    assert r.status_code == 400
    assert "not verified" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_send_customer_not_found(client: AsyncClient):
    r = await client.post("/customers/ghost/send", json={"subject": "X", "html": "<p>X</p>"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_send_no_subscribed_contacts(client: AsyncClient):
    """Sending to a customer with zero contacts returns 400."""
    info = await create_verified_customer(client)
    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "Empty",
        "html": "<p>empty</p>",
    })
    assert r.status_code == 400
    assert "no subscribed contacts" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_send_excludes_unsubscribed_contacts(client: AsyncClient):
    """Unsubscribed contacts must never receive emails."""
    info = await create_verified_customer(client)
    contact = await add_contact(client, info["id"])
    await client.patch(
        f"/customers/{info['id']}/contacts/{contact['id']}/unsubscribe"
    )
    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "Hi",
        "html": "<p>hi</p>",
    })
    # All contacts unsubscribed → no one to send to → 400
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_send_filtered_by_tag(client: AsyncClient):
    """Only contacts with the matching tag should receive the email."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"], email="premium@example.com", tags=["premium"])
    await add_contact(client, info["id"], email="free@example.com", tags=["free"])

    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "VIP only",
        "html": "<p>VIP content</p>",
        "tag": "premium",
    })
    assert r.status_code == 200
    assert r.json()["sent_to"] == 1


@pytest.mark.asyncio
async def test_send_tag_with_no_matching_contacts_returns_400(client: AsyncClient):
    info = await create_verified_customer(client)
    await add_contact(client, info["id"], email="free@example.com", tags=["free"])
    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "Hi",
        "html": "<p>hi</p>",
        "tag": "nonexistent-tag",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_send_filtered_by_segment(client: AsyncClient):
    """Segment targeting should limit sends to only contacts in that segment."""
    info = await create_verified_customer(client)
    c1 = await add_contact(client, info["id"], email="in@example.com")
    await add_contact(client, info["id"], email="out@example.com")

    seg_r = await client.post(f"/customers/{info['id']}/segments", json={"name": "VIP"})
    seg_id = seg_r.json()["id"]
    await client.post(
        f"/customers/{info['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [c1["id"]]}
    )

    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "Segment send",
        "html": "<p>for segment</p>",
        "segment_id": seg_id,
    })
    assert r.status_code == 200
    assert r.json()["sent_to"] == 1


@pytest.mark.asyncio
async def test_send_segment_not_found_returns_404(client: AsyncClient):
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])
    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "X",
        "html": "<p>x</p>",
        "segment_id": "nonexistent-segment-id",
    })
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_send_segment_id_takes_priority_over_tag(client: AsyncClient):
    """
    When both segment_id and tag are provided, segment_id wins.
    The tag is ignored.
    """
    info = await create_verified_customer(client)
    c1 = await add_contact(client, info["id"], email="seg@example.com", tags=["other"])
    await add_contact(client, info["id"], email="tag@example.com", tags=["premium"])

    seg_r = await client.post(f"/customers/{info['id']}/segments", json={"name": "S"})
    seg_id = seg_r.json()["id"]
    await client.post(
        f"/customers/{info['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [c1["id"]]}
    )

    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "X",
        "html": "<p>x</p>",
        "segment_id": seg_id,
        "tag": "premium",         # should be ignored
    })
    assert r.status_code == 200
    assert r.json()["sent_to"] == 1   # only c1, not the tagged one


@pytest.mark.asyncio
async def test_send_records_targeting_in_campaign(client: AsyncClient):
    """Campaign record should capture the targeting method used."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"], email="p@example.com", tags=["premium"])

    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": "Hi",
        "html": "<p>hi</p>",
        "tag": "premium",
    })
    campaign_id = r.json()["campaign_id"]
    cr = await client.get(f"/customers/{info['id']}/campaigns/{campaign_id}")
    assert cr.json()["targeting"] == {"tag": "premium"}


# ── POST /customers/{id}/send/single  (transactional) ────────────────────────

@pytest.mark.asyncio
async def test_send_single_success(client: AsyncClient):
    info = await create_verified_customer(client)
    r = await client.post(f"/customers/{info['id']}/send/single", json={
        "to_email": "recipient@example.com",
        "subject": "Your order has shipped",
        "html": "<p>It's on the way!</p>",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["to"] == "recipient@example.com"
    assert data["from"] == "hello@acme.com"
    assert data["email_id"] == "email_abc123"


@pytest.mark.asyncio
async def test_send_single_domain_not_verified(client: AsyncClient):
    c = await create_customer(client)
    r = await client.post(f"/customers/{c['id']}/send/single", json={
        "to_email": "x@example.com",
        "subject": "Hi",
        "html": "<p>hi</p>",
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_send_single_invalid_email(client: AsyncClient):
    info = await create_verified_customer(client)
    r = await client.post(f"/customers/{info['id']}/send/single", json={
        "to_email": "not-an-email",
        "subject": "Hi",
        "html": "<p>hi</p>",
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_send_single_customer_not_found(client: AsyncClient):
    r = await client.post("/customers/ghost/send/single", json={
        "to_email": "x@example.com",
        "subject": "Hi",
        "html": "<p>hi</p>",
    })
    assert r.status_code == 404


# ── Idempotency ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_idempotency_key_deduplicates_send(client: AsyncClient, db_session):
    """Same Idempotency-Key → send_batch called once; retry returns identical response."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])

    headers = {"Idempotency-Key": "idem-dedup-001"}
    payload = {"subject": "Hello", "html": "<p>Hi</p>"}

    r1 = await client.post(f"/customers/{info['id']}/send", json=payload, headers=headers)
    r2 = await client.post(f"/customers/{info['id']}/send", json=payload, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert r1.json()["campaign_id"] == "idem-dedup-001"

    # Only one Campaign row — retry didn't create a second one.
    result = await db_session.execute(
        select(Campaign).where(Campaign.id == "idem-dedup-001")
    )
    assert len(result.scalars().all()) == 1

    # Only one CampaignRecipient — no double-send.
    rows = (await db_session.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == "idem-dedup-001")
    )).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_partial_campaign_returns_409_on_retry(client: AsyncClient, db_session):
    """A partial campaign must return 409, not 200 — retrying would silently skip contacts."""
    info = await create_verified_customer(client)

    partial = Campaign(
        id="idem-partial-001",
        customer_id=info["id"],
        subject="Test",
        sent_to_count=50,
        from_address="hello@acme.com",
        targeting={},
        status="partial",
    )
    db_session.add(partial)
    await db_session.commit()

    r = await client.post(
        f"/customers/{info['id']}/send",
        json={"subject": "Test", "html": "<p>hi</p>"},
        headers={"Idempotency-Key": "idem-partial-001"},
    )

    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["campaign_status"] == "partial"
    assert body["sent_to"] == 50
    assert body["campaign_id"] == "idem-partial-001"


@pytest.mark.asyncio
async def test_sending_campaign_returns_409_on_retry(client: AsyncClient, db_session):
    """A campaign stuck in 'sending' (crashed mid-loop) also returns 409."""
    info = await create_verified_customer(client)

    stuck = Campaign(
        id="idem-sending-001",
        customer_id=info["id"],
        subject="Test",
        sent_to_count=0,
        from_address="hello@acme.com",
        targeting={},
        status="sending",
    )
    db_session.add(stuck)
    await db_session.commit()

    r = await client.post(
        f"/customers/{info['id']}/send",
        json={"subject": "Test", "html": "<p>hi</p>"},
        headers={"Idempotency-Key": "idem-sending-001"},
    )

    assert r.status_code == 409
    assert r.json()["detail"]["campaign_status"] == "sending"


@pytest.mark.asyncio
async def test_single_email_passes_idempotency_key(client: AsyncClient):
    """The Idempotency-Key header is forwarded to send_single."""
    from app.services.resend_service import SendResult
    info = await create_verified_customer(client)

    mock = AsyncMock(return_value={"id": "sid_idem_001"})
    with patch("app.routers.email.send_single", mock):
        r = await client.post(
            f"/customers/{info['id']}/send/single",
            json={"to_email": "x@example.com", "subject": "Hi", "html": "<p>hi</p>"},
            headers={"Idempotency-Key": "single-idem-001"},
        )

    assert r.status_code == 200
    assert r.json()["email_id"] == "sid_idem_001"
    _, kwargs = mock.call_args
    assert kwargs.get("idempotency_key") == "single-idem-001"


# ── Resume from partial campaign ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_skips_already_emailed_contacts(client: AsyncClient, db_session):
    """
    resume_from_campaign_id excludes contacts already in the failed campaign's
    recipient list — only the contacts who were NOT emailed get the retry send.
    """
    info = await create_verified_customer(client)
    c1 = await add_contact(client, info["id"], email="first@example.com")
    c2 = await add_contact(client, info["id"], email="second@example.com")

    # Simulate a partial campaign that already emailed c1 but not c2.
    partial = Campaign(
        id="resume-partial-001",
        customer_id=info["id"],
        subject="Original",
        sent_to_count=1,
        from_address="hello@acme.com",
        targeting={},
        status="partial",
    )
    db_session.add(partial)
    db_session.add(CampaignRecipient(
        campaign_id="resume-partial-001",
        contact_id=c1["id"],
        resend_email_id="eid_already_sent",
        status="queued",
    ))
    await db_session.commit()

    r = await client.post(
        f"/customers/{info['id']}/send",
        json={
            "subject": "Retry",
            "html": "<p>retry</p>",
            "resume_from_campaign_id": "resume-partial-001",
        },
        headers={"Idempotency-Key": "resume-retry-001"},
    )

    assert r.status_code == 200
    data = r.json()
    # Only c2 should have been sent to — c1 was already in the partial campaign.
    assert data["sent_to"] == 1

    # Confirm only one new CampaignRecipient was created (for c2).
    rows = (await db_session.execute(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == "resume-retry-001")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].contact_id == c2["id"]


@pytest.mark.asyncio
async def test_resume_rejects_non_partial_statuses(client: AsyncClient, db_session):
    """Only 'partial' campaigns can be resumed — 'sent' and 'sending' are rejected."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])

    for status, campaign_id, idem_key in [
        ("sent",    "resume-status-sent-001",    "resume-retry-sent"),
        ("sending", "resume-status-sending-001", "resume-retry-sending"),
    ]:
        campaign = Campaign(
            id=campaign_id,
            customer_id=info["id"],
            subject="Test",
            sent_to_count=0,
            from_address="hello@acme.com",
            targeting={},
            status=status,
        )
        db_session.add(campaign)
        await db_session.commit()

        r = await client.post(
            f"/customers/{info['id']}/send",
            json={
                "subject": "Retry",
                "html": "<p>retry</p>",
                "resume_from_campaign_id": campaign_id,
            },
            headers={"Idempotency-Key": idem_key},
        )
        assert r.status_code == 400, f"expected 400 for status={status}, got {r.status_code}"
        assert status in r.json()["detail"]


@pytest.mark.asyncio
async def test_resume_from_nonexistent_campaign_returns_404(client: AsyncClient):
    """Passing an unknown resume_from_campaign_id returns 404."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])

    r = await client.post(
        f"/customers/{info['id']}/send",
        json={
            "subject": "Retry",
            "html": "<p>retry</p>",
            "resume_from_campaign_id": "does-not-exist",
        },
        headers={"Idempotency-Key": "resume-retry-003"},
    )
    assert r.status_code == 404
