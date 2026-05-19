"""
Tests for campaign history endpoints.

Endpoints covered:
  GET    /customers/{id}/campaigns                    list_campaigns
  GET    /customers/{id}/campaigns/{cid}              get_campaign
  DELETE /customers/{id}/campaigns/{cid}              delete_campaign

Campaign rows are created as a side-effect of POST /customers/{id}/send,
so we trigger that first to seed data for these tests.
"""
import pytest
from httpx import AsyncClient
from tests.conftest import create_verified_customer, add_contact


async def _seed_campaign(client: AsyncClient, subject: str = "Test subject") -> dict:
    """Helper: create a verified customer, add a contact, send a campaign."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])
    r = await client.post(f"/customers/{info['id']}/send", json={
        "subject": subject,
        "html": "<p>Body</p>",
    })
    assert r.status_code == 200
    return {"customer_id": info["id"], "campaign_id": r.json()["campaign_id"]}


# ── GET /customers/{id}/campaigns ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_campaigns_empty(client: AsyncClient):
    info = await create_verified_customer(client)
    r = await client.get(f"/customers/{info['id']}/campaigns")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_campaigns_returns_all(client: AsyncClient):
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])

    for subject in ("First send", "Second send", "Third send"):
        await client.post(f"/customers/{info['id']}/send", json={
            "subject": subject,
            "html": "<p>body</p>",
        })

    r = await client.get(f"/customers/{info['id']}/campaigns")
    assert r.status_code == 200
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_list_campaigns_ordered_newest_first(client: AsyncClient):
    """The list must come back in descending sent_at order."""
    info = await create_verified_customer(client)
    await add_contact(client, info["id"])

    for subject in ("Older", "Newer"):
        await client.post(f"/customers/{info['id']}/send", json={
            "subject": subject, "html": "<p>x</p>",
        })

    r = await client.get(f"/customers/{info['id']}/campaigns")
    subjects = [c["subject"] for c in r.json()]
    assert subjects[0] == "Newer"


@pytest.mark.asyncio
async def test_list_campaigns_includes_expected_fields(client: AsyncClient):
    seed = await _seed_campaign(client, "My Campaign")
    r = await client.get(f"/customers/{seed['customer_id']}/campaigns")
    item = r.json()[0]
    for field in ("id", "subject", "sent_to_count", "from_address", "targeting", "status", "sent_at"):
        assert field in item, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_list_campaigns_isolated_per_customer(client: AsyncClient):
    """Campaigns from customer A must not appear for customer B."""
    seed = await _seed_campaign(client)
    info_b = await create_verified_customer(client, "Other Corp")
    r = await client.get(f"/customers/{info_b['id']}/campaigns")
    assert r.json() == []


# ── GET /customers/{id}/campaigns/{cid} ───────────────────────────────────────

@pytest.mark.asyncio
async def test_get_campaign_success(client: AsyncClient):
    seed = await _seed_campaign(client, "Specific Campaign")
    r = await client.get(
        f"/customers/{seed['customer_id']}/campaigns/{seed['campaign_id']}"
    )
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == seed["campaign_id"]
    assert data["subject"] == "Specific Campaign"
    assert data["status"] == "sent"


@pytest.mark.asyncio
async def test_get_campaign_not_found(client: AsyncClient):
    info = await create_verified_customer(client)
    r = await client.get(f"/customers/{info['id']}/campaigns/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_campaign_wrong_customer(client: AsyncClient):
    """Cannot retrieve a campaign belonging to a different customer."""
    seed = await _seed_campaign(client)
    info_b = await create_verified_customer(client, "Corp B")
    r = await client.get(
        f"/customers/{info_b['id']}/campaigns/{seed['campaign_id']}"
    )
    assert r.status_code == 404


# ── DELETE /customers/{id}/campaigns/{cid} ────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_campaign_success(client: AsyncClient):
    seed = await _seed_campaign(client)
    r = await client.delete(
        f"/customers/{seed['customer_id']}/campaigns/{seed['campaign_id']}"
    )
    assert r.status_code == 200
    data = r.json()
    assert data["deleted"] is True
    assert data["campaign_id"] == seed["campaign_id"]


@pytest.mark.asyncio
async def test_delete_campaign_removes_from_list(client: AsyncClient):
    seed = await _seed_campaign(client)
    await client.delete(
        f"/customers/{seed['customer_id']}/campaigns/{seed['campaign_id']}"
    )
    r = await client.get(f"/customers/{seed['customer_id']}/campaigns")
    assert r.json() == []


@pytest.mark.asyncio
async def test_delete_campaign_not_found(client: AsyncClient):
    info = await create_verified_customer(client)
    r = await client.delete(f"/customers/{info['id']}/campaigns/ghost")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_campaign_wrong_customer(client: AsyncClient):
    seed = await _seed_campaign(client)
    info_b = await create_verified_customer(client, "Corp B")
    r = await client.delete(
        f"/customers/{info_b['id']}/campaigns/{seed['campaign_id']}"
    )
    assert r.status_code == 404
