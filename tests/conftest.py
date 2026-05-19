"""
Shared fixtures for the entire test suite.

Key design decisions:
  - Uses an in-memory SQLite database (sqlite+aiosqlite:///:memory:) so tests
    are fully isolated — no file left behind, no shared state between runs.
  - All resend_service functions are patched at the module level so no real
    Resend API key, domain, or webhook is ever required.
  - A single httpx.AsyncClient is provided per test via the `client` fixture.
"""
import pytest
import pytest_asyncio
from typing import Optional, List
from unittest.mock import patch, MagicMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.main import app
from app.database import get_db
from app.models import Base


# ── In-memory test database ───────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """
    Creates a fresh in-memory database for every test function.
    Tables are created before the test and dropped after — fully isolated.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Override get_db dependency ────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession):
    """
    Returns an httpx.AsyncClient wired to the FastAPI app with the
    test database injected. All resend_service calls are also mocked
    here so nothing reaches the real Resend API.
    """
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    mock_add_domain = MagicMock(return_value={
        "id": "dom_test123",
        "status": "not_started",
        "records": [
            {"type": "MX",  "name": "send", "value": "feedback-smtp.us-east-1.amazonses.com", "priority": 10},
            {"type": "TXT", "name": "send", "value": "v=spf1 include:amazonses.com ~all"},
        ],
    })
    mock_verify_domain = MagicMock(return_value={"id": "dom_test123"})
    mock_get_domain_status_verified = MagicMock(return_value={"status": "verified"})
    mock_send_single = MagicMock(return_value={"id": "email_abc123"})
    mock_send_bulk = MagicMock(return_value=[{"data": [{"id": "e1"}, {"id": "e2"}]}])

    with (
        patch("app.routers.customers.resend_service.add_domain", mock_add_domain),
        patch("app.routers.customers.resend_service.verify_domain", mock_verify_domain),
        patch("app.routers.customers.resend_service.get_domain_status", mock_get_domain_status_verified),
        patch("app.routers.email.send_single", mock_send_single),
        patch("app.routers.email.send_bulk", mock_send_bulk),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


# ── Convenience helpers ───────────────────────────────────────────────────────

async def create_customer(client: AsyncClient, name: str = "Test Corp") -> dict:
    """Helper: create a customer and return the response JSON."""
    r = await client.post("/customers/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


async def create_verified_customer(client: AsyncClient, name: str = "Verified Corp") -> dict:
    """
    Helper: create a customer and simulate a verified domain so send
    endpoints are available during tests.
    """
    c = await create_customer(client, name)
    cid = c["id"]
    # Register domain (mocked — returns dom_test123, status not_started)
    r = await client.post(f"/customers/{cid}/domains", json={"domain_name": "acme.com"})
    assert r.status_code == 200, r.text
    # Verify domain (mocked get_domain_status returns "verified")
    r = await client.post(f"/customers/{cid}/domains/verify")
    assert r.status_code == 200, r.text
    return {"id": cid, "domain": "acme.com"}


async def add_contact(
    client: AsyncClient,
    customer_id: str,
    email: str = "alice@example.com",
    first_name: str = "Alice",
    tags: Optional[List[str]] = None,
) -> dict:
    """Helper: add a contact to a customer."""
    payload = {"email": email, "first_name": first_name}
    if tags:
        payload["tags"] = tags
    r = await client.post(f"/customers/{customer_id}/contacts", json=payload)
    assert r.status_code == 200, r.text
    return r.json()
