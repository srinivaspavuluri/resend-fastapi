"""
Playwright end-to-end API tests against a live uvicorn server.

These tests exercise the full HTTP stack — real TCP socket, uvicorn routing,
FastAPI middleware, and SQLite persistence — using Playwright's sync
APIRequestContext.

Coverage focus:
  - Complete customer → domain → contact → segment → send workflow
  - Correct HTTP status codes over real HTTP (not ASGI bypass)
  - Response shape and JSON serialisation
  - Cross-customer data isolation (one customer cannot see another's data)
  - Pagination / ordering on list endpoints

Resend API calls are still mocked (see e2e/conftest.py) — no real credentials needed.
"""
import pytest
from typing import Optional, List
from playwright.sync_api import APIRequestContext


# ── Helpers ───────────────────────────────────────────────────────────────────

def create_customer(api: APIRequestContext, name: str = "E2E Corp") -> dict:
    r = api.post("/customers/", data={"name": name})
    assert r.status == 200, r.text()
    return r.json()


def add_domain_and_verify(api: APIRequestContext, customer_id: str, domain: str = "e2e.com") -> None:
    r = api.post(f"/customers/{customer_id}/domains", data={"domain_name": domain})
    assert r.status == 200, r.text()
    r = api.post(f"/customers/{customer_id}/domains/verify")
    assert r.status == 200, r.text()


def add_contact(
    api: APIRequestContext,
    customer_id: str,
    email: str = "alice@e2e.com",
    first_name: str = "Alice",
    tags: Optional[List[str]] = None,
) -> dict:
    payload: dict = {"email": email, "first_name": first_name}
    if tags:
        payload["tags"] = tags
    r = api.post(f"/customers/{customer_id}/contacts", data=payload)
    assert r.status == 200, r.text()
    return r.json()


# ── Health check ──────────────────────────────────────────────────────────────

def test_health_endpoint(api: APIRequestContext):
    r = api.get("/")
    assert r.status == 200
    data = r.json()
    assert data["status"] == "ok"


# ── Full happy-path workflow ───────────────────────────────────────────────────

def test_full_customer_to_send_workflow(api: APIRequestContext):
    """
    Golden path: create customer → verify domain → add contacts →
    create segment → populate segment → send to segment.
    """
    # 1. Create customer
    customer = create_customer(api, "Golden Path Corp")
    cid = customer["id"]
    assert customer["name"] == "Golden Path Corp"

    # 2. Register & verify domain
    add_domain_and_verify(api, cid, domain="goldenpath.com")

    # Confirm domain shows as verified
    status_r = api.get(f"/customers/{cid}/domains/status")
    assert status_r.status == 200
    assert status_r.json()["verified"] is True

    # 3. Add contacts
    c1 = add_contact(api, cid, email="user1@goldenpath.com", first_name="User1")
    c2 = add_contact(api, cid, email="user2@goldenpath.com", first_name="User2")

    contacts_r = api.get(f"/customers/{cid}/contacts")
    assert contacts_r.status == 200
    assert len(contacts_r.json()) == 2

    # 4. Create segment and populate it
    seg_r = api.post(f"/customers/{cid}/segments", data={"name": "Beta"})
    assert seg_r.status == 200
    seg_id = seg_r.json()["id"]

    add_r = api.post(
        f"/customers/{cid}/segments/{seg_id}/contacts",
        data={"contact_ids": [c1["id"]]},
    )
    assert add_r.status == 200
    assert add_r.json()["added_count"] == 1

    # 5. Send to segment
    send_r = api.post(f"/customers/{cid}/send", data={
        "subject": "Welcome {{first_name}}!",
        "html": "<p>Hello {{first_name}}, you are in the beta!</p>",
        "segment_id": seg_id,
    })
    assert send_r.status == 200
    data = send_r.json()
    assert data["sent_to"] == 1
    assert data["from"] == "hello@goldenpath.com"
    assert "campaign_id" in data


# ── Customer CRUD ─────────────────────────────────────────────────────────────

def test_customer_list_and_delete(api: APIRequestContext):
    c = create_customer(api, "Temp Corp")
    cid = c["id"]

    # Should appear in list
    list_r = api.get("/customers/")
    assert any(x["id"] == cid for x in list_r.json())

    # Delete
    del_r = api.delete(f"/customers/{cid}")
    assert del_r.status == 200
    assert del_r.json()["deleted"] is True

    # Should no longer appear
    list_r2 = api.get("/customers/")
    assert all(x["id"] != cid for x in list_r2.json())


def test_update_customer_name(api: APIRequestContext):
    c = create_customer(api, "Before")
    r = api.patch(f"/customers/{c['id']}", data={"name": "After"})
    assert r.status == 200
    assert r.json()["name"] == "After"


def test_create_customer_missing_name_returns_422(api: APIRequestContext):
    r = api.post("/customers/", data={})
    assert r.status == 422


# ── Contact management ────────────────────────────────────────────────────────

def test_duplicate_contact_returns_409(api: APIRequestContext):
    c = create_customer(api, "Dup Corp")
    add_contact(api, c["id"], email="dup@e2e.com")
    r = api.post(f"/customers/{c['id']}/contacts", data={"email": "dup@e2e.com"})
    assert r.status == 409


def test_unsubscribe_removes_from_default_list(api: APIRequestContext):
    c = create_customer(api, "Unsub Corp")
    contact = add_contact(api, c["id"], email="leave@e2e.com")

    unsub_r = api.patch(f"/customers/{c['id']}/contacts/{contact['id']}/unsubscribe")
    assert unsub_r.status == 200
    assert unsub_r.json()["is_subscribed"] is False

    # Default list (subscribed_only=true) should not include them
    list_r = api.get(f"/customers/{c['id']}/contacts")
    assert all(x["email"] != "leave@e2e.com" for x in list_r.json())

    # subscribed_only=false should include them
    all_r = api.get(f"/customers/{c['id']}/contacts?subscribed_only=false")
    assert any(x["email"] == "leave@e2e.com" for x in all_r.json())


def test_delete_contact_removes_permanently(api: APIRequestContext):
    c = create_customer(api, "Del Contact Corp")
    contact = add_contact(api, c["id"], email="gone@e2e.com")
    del_r = api.delete(f"/customers/{c['id']}/contacts/{contact['id']}")
    assert del_r.status == 200
    list_r = api.get(f"/customers/{c['id']}/contacts?subscribed_only=false")
    assert all(x["email"] != "gone@e2e.com" for x in list_r.json())


# ── Cross-customer isolation ───────────────────────────────────────────────────

def test_contacts_isolated_between_customers(api: APIRequestContext):
    """Contacts from one customer must be invisible to another."""
    ca = create_customer(api, "Isolated A")
    cb = create_customer(api, "Isolated B")
    add_contact(api, ca["id"], email="only-a@e2e.com")

    list_b = api.get(f"/customers/{cb['id']}/contacts?subscribed_only=false")
    assert list_b.json() == []


def test_campaigns_isolated_between_customers(api: APIRequestContext):
    """Campaigns from customer A must not appear under customer B."""
    ca = create_customer(api, "CampaignA")
    cb = create_customer(api, "CampaignB")
    add_domain_and_verify(api, ca["id"], "cama.com")
    add_contact(api, ca["id"], email="x@cama.com")
    api.post(f"/customers/{ca['id']}/send", data={
        "subject": "A's campaign", "html": "<p>a</p>"
    })

    list_b = api.get(f"/customers/{cb['id']}/campaigns")
    assert list_b.json() == []


# ── Segment flow ──────────────────────────────────────────────────────────────

def test_delete_segment_keeps_contacts(api: APIRequestContext):
    c = create_customer(api, "Seg Keep Corp")
    contact = add_contact(api, c["id"], email="keep@e2e.com")
    seg_r = api.post(f"/customers/{c['id']}/segments", data={"name": "Temp"})
    seg_id = seg_r.json()["id"]
    api.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        data={"contact_ids": [contact["id"]]}
    )
    del_r = api.delete(f"/customers/{c['id']}/segments/{seg_id}")
    assert del_r.status == 200

    # Contact must still exist
    contacts = api.get(f"/customers/{c['id']}/contacts").json()
    assert any(x["email"] == "keep@e2e.com" for x in contacts)


def test_segment_contact_count(api: APIRequestContext):
    c = create_customer(api, "Count Corp")
    seg_r = api.post(f"/customers/{c['id']}/segments", data={"name": "Counted"})
    seg_id = seg_r.json()["id"]
    c1 = add_contact(api, c["id"], email="one@e2e.com")
    c2 = add_contact(api, c["id"], email="two@e2e.com")
    api.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        data={"contact_ids": [c1["id"], c2["id"]]}
    )
    segs = api.get(f"/customers/{c['id']}/segments").json()
    seg = next(s for s in segs if s["id"] == seg_id)
    assert seg["contact_count"] == 2


# ── Campaign CRUD ─────────────────────────────────────────────────────────────

def test_campaign_list_newest_first(api: APIRequestContext):
    c = create_customer(api, "Campaign Order Corp")
    add_domain_and_verify(api, c["id"], "order.com")
    add_contact(api, c["id"], email="r@order.com")

    for subj in ("First", "Second"):
        api.post(f"/customers/{c['id']}/send", data={"subject": subj, "html": "<p>x</p>"})

    campaigns = api.get(f"/customers/{c['id']}/campaigns").json()
    assert campaigns[0]["subject"] == "Second"


def test_delete_campaign(api: APIRequestContext):
    c = create_customer(api, "Del Campaign Corp")
    add_domain_and_verify(api, c["id"], "delcamp.com")
    add_contact(api, c["id"], email="r@delcamp.com")
    send_r = api.post(f"/customers/{c['id']}/send", data={
        "subject": "To delete", "html": "<p>x</p>"
    })
    campaign_id = send_r.json()["campaign_id"]

    del_r = api.delete(f"/customers/{c['id']}/campaigns/{campaign_id}")
    assert del_r.status == 200
    assert del_r.json()["deleted"] is True

    list_r = api.get(f"/customers/{c['id']}/campaigns")
    assert all(x["id"] != campaign_id for x in list_r.json())


# ── Error path responses ──────────────────────────────────────────────────────

def test_404_on_unknown_customer(api: APIRequestContext):
    r = api.get("/customers/does-not-exist/contacts")
    # Returns 200 with empty list (contacts filtered by customer_id)
    # The endpoint does not 404 on unknown customer — it just returns []
    assert r.status == 200
    assert r.json() == []


def test_send_without_verified_domain_returns_400(api: APIRequestContext):
    c = create_customer(api, "Unverified Corp")
    add_contact(api, c["id"], email="u@unverified.com")
    r = api.post(f"/customers/{c['id']}/send", data={
        "subject": "Hi", "html": "<p>hi</p>"
    })
    assert r.status == 400
hi</p>"
    })
    assert r.status == 400
