"""
Tests for the Resend webhook handler.

Endpoint covered:
  POST /webhooks/resend

Tests cover:
  - All six event types (sent, delivered, opened, clicked, bounced, complained)
  - Unknown event types are accepted (not rejected)
  - Signature verification (valid / invalid / missing headers)
  - Dev mode (no WEBHOOK_SECRET set) — all requests accepted
  - Always returns HTTP 200 so Resend does not retry

Patching strategy:
  The handler now reads the secret per-request via _get_secret() instead of a
  module-level constant. Tests patch _get_secret to return a controlled value:
    patch("app.routers.webhooks._get_secret", return_value="")        # no secret
    patch("app.routers.webhooks._get_secret", return_value=SECRET)    # with secret
"""
import base64
import hashlib
import hmac
import json
import pytest
from typing import Optional
from unittest.mock import patch
from httpx import AsyncClient


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_svix_headers(payload: bytes, secret: str, svix_id: str = "msg_test01") -> dict:
    """
    Generate valid Svix signature headers for a given payload and secret.
    Mirrors the logic in app/routers/webhooks.py::_verify_svix_signature.
    """
    svix_timestamp = "1713000000"

    if secret.startswith("whsec_"):
        secret_bytes = base64.b64decode(secret[6:])
    else:
        secret_bytes = secret.encode()

    signed_content = f"{svix_id}.{svix_timestamp}.".encode() + payload
    digest = hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
    sig_b64 = base64.b64encode(digest).decode()

    return {
        "svix-id": svix_id,
        "svix-timestamp": svix_timestamp,
        "svix-signature": f"v1,{sig_b64}",
    }


def _event(event_type: str, data: Optional[dict] = None) -> bytes:
    """Build a minimal Resend webhook event payload."""
    return json.dumps({"type": event_type, "data": data or {}}).encode()


VALID_SECRET = "whsec_" + base64.b64encode(b"supersecretkey1234567890").decode()


def _no_secret():
    """Patch helper — simulates WEBHOOK_SECRET not being set."""
    return patch("app.routers.webhooks._get_secret", return_value="")


def _with_secret(secret: str = VALID_SECRET):
    """Patch helper — simulates WEBHOOK_SECRET being set to `secret`."""
    return patch("app.routers.webhooks._get_secret", return_value=secret)


# ── Dev mode (no secret) ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_accepted_without_secret(client: AsyncClient):
    """When WEBHOOK_SECRET is not set, all requests are accepted."""
    with _no_secret():
        payload = _event("email.sent", {"email_id": "e1", "to": ["x@example.com"]})
        r = await client.post(
            "/webhooks/resend",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 200
    assert r.json()["received"] is True


# ── Event routing — all six types ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,data", [
    ("email.sent",       {"email_id": "e1", "to": ["a@example.com"]}),
    ("email.delivered",  {"email_id": "e2"}),
    ("email.opened",     {"email_id": "e3"}),
    ("email.clicked",    {"email_id": "e4", "click": {"link": "https://example.com"}}),
    ("email.bounced",    {"to": ["bounce@example.com"]}),
    ("email.complained", {"to": ["spam@example.com"]}),
])
async def test_webhook_handles_event_type(
    client: AsyncClient,
    event_type: str,
    data: dict,
):
    with _no_secret():
        payload = _event(event_type, data)
        r = await client.post(
            "/webhooks/resend",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 200
    assert r.json()["type"] == event_type


@pytest.mark.asyncio
async def test_webhook_unknown_event_type_accepted(client: AsyncClient):
    """Unrecognised events should be accepted (200) and not raise an error."""
    with _no_secret():
        payload = _event("email.future_event", {"some": "data"})
        r = await client.post(
            "/webhooks/resend",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 200


# ── Signature verification ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_valid_signature_accepted(client: AsyncClient):
    payload = _event("email.sent", {"email_id": "e5"})
    headers = _make_svix_headers(payload, VALID_SECRET)
    headers["Content-Type"] = "application/json"

    with _with_secret(VALID_SECRET):
        r = await client.post("/webhooks/resend", content=payload, headers=headers)

    assert r.status_code == 200
    assert r.json()["received"] is True


@pytest.mark.asyncio
async def test_webhook_invalid_signature_rejected(client: AsyncClient):
    """A tampered signature must return 401."""
    payload = _event("email.sent", {"email_id": "e6"})
    headers = {
        "svix-id": "msg_tampered",
        "svix-timestamp": "1713000000",
        "svix-signature": "v1,aW52YWxpZHNpZ25hdHVyZQ==",
        "Content-Type": "application/json",
    }

    with _with_secret(VALID_SECRET):
        r = await client.post("/webhooks/resend", content=payload, headers=headers)

    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_missing_svix_headers_returns_400(client: AsyncClient):
    """When secret is set, missing Svix headers must return 400."""
    payload = _event("email.sent")
    with _with_secret(VALID_SECRET):
        r = await client.post(
            "/webhooks/resend",
            content=payload,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 400
    assert "svix" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_webhook_signature_with_plain_secret(client: AsyncClient):
    """Secrets without 'whsec_' prefix should also work (raw bytes)."""
    plain_secret = "plaintextsecret"
    payload = _event("email.delivered", {"email_id": "e7"})
    headers = _make_svix_headers(payload, plain_secret)
    headers["Content-Type"] = "application/json"

    with _with_secret(plain_secret):
        r = await client.post("/webhooks/resend", content=payload, headers=headers)

    assert r.status_code == 200


@pytest.mark.asyncio
async def test_webhook_always_returns_200_for_valid_requests(client: AsyncClient):
    """
    Resend retries on any non-2xx. Verify the handler never returns an error
    for a valid, properly signed request regardless of event type.
    """
    for event_type in ("email.sent", "email.bounced", "email.complained", "email.unknown_future"):
        payload = _event(event_type, {"email_id": "ex"})
        headers = _make_svix_headers(payload, VALID_SECRET)
        headers["Content-Type"] = "application/json"

        with _with_secret(VALID_SECRET):
            r = await client.post("/webhooks/resend", content=payload, headers=headers)

        assert r.status_code == 200, f"Expected 200 for {event_type}, got {r.status_code}"
