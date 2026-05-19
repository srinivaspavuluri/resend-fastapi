"""
Tests for customer CRUD and domain management endpoints.

Endpoints covered:
  POST   /customers/                      create_customer
  GET    /customers/                      list_customers
  PATCH  /customers/{id}                  update_customer
  DELETE /customers/{id}                  delete_customer
  POST   /customers/{id}/domains          add_domain  (Step 1)
  POST   /customers/{id}/domains/verify   verify_domain  (Step 2)
  GET    /customers/{id}/domains/status   domain_status

All Resend API calls are mocked in conftest.py — no real API key needed.
"""
import pytest
from httpx import AsyncClient
from tests.conftest import create_customer, create_verified_customer


# ── POST /customers/ ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_customer_success(client: AsyncClient):
    r = await client.post("/customers/", json={"name": "Acme Corp"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Acme Corp"
    assert "id" in data


@pytest.mark.asyncio
async def test_create_customer_missing_name(client: AsyncClient):
    r = await client.post("/customers/", json={})
    assert r.status_code == 422  # Pydantic validation error


@pytest.mark.asyncio
async def test_create_multiple_customers(client: AsyncClient):
    await client.post("/customers/", json={"name": "Corp A"})
    await client.post("/customers/", json={"name": "Corp B"})
    r = await client.get("/customers/")
    assert r.status_code == 200
    assert len(r.json()) == 2


# ── GET /customers/ ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_customers_empty(client: AsyncClient):
    r = await client.get("/customers/")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_customers_includes_domain_info(client: AsyncClient):
    c = await create_customer(client, "Acme")
    r = await client.get("/customers/")
    assert r.status_code == 200
    item = r.json()[0]
    assert item["id"] == c["id"]
    assert item["domain"] is None           # no domain yet
    assert item["domain_verified"] is False


# ── PATCH /customers/{id} ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_customer_name(client: AsyncClient):
    c = await create_customer(client, "OldName")
    r = await client.patch(f"/customers/{c['id']}", json={"name": "NewName"})
    assert r.status_code == 200
    assert r.json()["name"] == "NewName"


@pytest.mark.asyncio
async def test_update_customer_not_found(client: AsyncClient):
    r = await client.patch("/customers/nonexistent-id", json={"name": "X"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_update_customer_empty_body_no_error(client: AsyncClient):
    """Passing all nulls is allowed — returns customer unchanged."""
    c = await create_customer(client, "Stable Corp")
    r = await client.patch(f"/customers/{c['id']}", json={})
    assert r.status_code == 200
    assert r.json()["name"] == "Stable Corp"


# ── DELETE /customers/{id} ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_customer_success(client: AsyncClient):
    c = await create_customer(client, "To Delete")
    r = await client.delete(f"/customers/{c['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["deleted"] is True
    assert data["customer_id"] == c["id"]


@pytest.mark.asyncio
async def test_delete_customer_not_found(client: AsyncClient):
    r = await client.delete("/customers/ghost-id")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_customer_removes_from_list(client: AsyncClient):
    c = await create_customer(client)
    await client.delete(f"/customers/{c['id']}")
    r = await client.get("/customers/")
    assert r.json() == []


# ── POST /customers/{id}/domains  (Step 1) ────────────────────────────────────

@pytest.mark.asyncio
async def test_add_domain_success(client: AsyncClient):
    c = await create_customer(client)
    r = await client.post(f"/customers/{c['id']}/domains", json={"domain_name": "acme.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["domain"] == "acme.com"
    assert data["resend_domain_id"] == "dom_test123"
    assert "dns_records" in data
    assert "next_step" in data


@pytest.mark.asyncio
async def test_add_domain_customer_not_found(client: AsyncClient):
    r = await client.post("/customers/ghost/domains", json={"domain_name": "acme.com"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_add_domain_missing_domain_name(client: AsyncClient):
    c = await create_customer(client)
    r = await client.post(f"/customers/{c['id']}/domains", json={})
    assert r.status_code == 422


# ── POST /customers/{id}/domains/verify  (Step 2) ─────────────────────────────

@pytest.mark.asyncio
async def test_verify_domain_success(client: AsyncClient):
    """Mock returns status=verified, so domain_verified should flip to True."""
    c = await create_customer(client)
    await client.post(f"/customers/{c['id']}/domains", json={"domain_name": "acme.com"})
    r = await client.post(f"/customers/{c['id']}/domains/verify")
    assert r.status_code == 200
    data = r.json()
    assert data["verified"] is True
    assert data["status"] == "verified"


@pytest.mark.asyncio
async def test_verify_domain_no_domain_registered(client: AsyncClient):
    """Cannot verify before calling /domains."""
    c = await create_customer(client)
    r = await client.post(f"/customers/{c['id']}/domains/verify")
    assert r.status_code == 400
    assert "No domain registered" in r.json()["detail"]


@pytest.mark.asyncio
async def test_verify_domain_customer_not_found(client: AsyncClient):
    r = await client.post("/customers/ghost/domains/verify")
    assert r.status_code == 404


# ── GET /customers/{id}/domains/status ────────────────────────────────────────

@pytest.mark.asyncio
async def test_domain_status_verified(client: AsyncClient):
    """After a full domain flow, status endpoint reflects verified."""
    info = await create_verified_customer(client)
    r = await client.get(f"/customers/{info['id']}/domains/status")
    assert r.status_code == 200
    data = r.json()
    assert data["verified"] is True
    assert data["domain"] == "acme.com"


@pytest.mark.asyncio
async def test_domain_status_no_domain(client: AsyncClient):
    c = await create_customer(client)
    r = await client.get(f"/customers/{c['id']}/domains/status")
    assert r.status_code == 404
