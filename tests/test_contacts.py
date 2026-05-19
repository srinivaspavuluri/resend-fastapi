"""
Tests for contacts CRUD, unsubscribe, and segment management.

Endpoints covered:
  POST   /customers/{id}/contacts                              add_contact
  GET    /customers/{id}/contacts                             list_contacts
  PATCH  /customers/{id}/contacts/{cid}                       update_contact
  DELETE /customers/{id}/contacts/{cid}                       delete_contact
  PATCH  /customers/{id}/contacts/{cid}/unsubscribe           unsubscribe_contact
  POST   /customers/{id}/segments                             create_segment
  GET    /customers/{id}/segments                             list_segments
  GET    /customers/{id}/segments/{sid}                       get_segment
  PATCH  /customers/{id}/segments/{sid}                       update_segment
  DELETE /customers/{id}/segments/{sid}                       delete_segment
  POST   /customers/{id}/segments/{sid}/contacts              add_contacts_to_segment
  DELETE /customers/{id}/segments/{sid}/contacts/{cid}        remove_contact_from_segment
"""
import pytest
from httpx import AsyncClient
from tests.conftest import create_customer, add_contact


# ── POST /customers/{id}/contacts ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_contact_success(client: AsyncClient):
    c = await create_customer(client)
    r = await client.post(f"/customers/{c['id']}/contacts", json={
        "email": "alice@example.com",
        "first_name": "Alice",
        "tags": ["newsletter"],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == "alice@example.com"
    assert data["first_name"] == "Alice"
    assert "newsletter" in data["tags"]
    assert "id" in data


@pytest.mark.asyncio
async def test_add_contact_duplicate_email(client: AsyncClient):
    c = await create_customer(client)
    await add_contact(client, c["id"], email="dup@example.com")
    r = await client.post(f"/customers/{c['id']}/contacts", json={"email": "dup@example.com"})
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


@pytest.mark.asyncio
async def test_add_contact_invalid_email(client: AsyncClient):
    c = await create_customer(client)
    r = await client.post(f"/customers/{c['id']}/contacts", json={"email": "not-an-email"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_add_contact_customer_not_found(client: AsyncClient):
    r = await client.post("/customers/ghost/contacts", json={"email": "x@example.com"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_same_email_allowed_for_different_customers(client: AsyncClient):
    """The same email address can appear under two different customers."""
    c1 = await create_customer(client, "Corp A")
    c2 = await create_customer(client, "Corp B")
    await add_contact(client, c1["id"], email="shared@example.com")
    r = await client.post(f"/customers/{c2['id']}/contacts", json={"email": "shared@example.com"})
    assert r.status_code == 200


# ── GET /customers/{id}/contacts ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_contacts_empty(client: AsyncClient):
    c = await create_customer(client)
    r = await client.get(f"/customers/{c['id']}/contacts")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_contacts_returns_all_subscribed(client: AsyncClient):
    c = await create_customer(client)
    await add_contact(client, c["id"], email="a@example.com")
    await add_contact(client, c["id"], email="b@example.com")
    r = await client.get(f"/customers/{c['id']}/contacts")
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_list_contacts_filter_by_tag(client: AsyncClient):
    c = await create_customer(client)
    await add_contact(client, c["id"], email="premium@example.com", tags=["premium"])
    await add_contact(client, c["id"], email="free@example.com", tags=["free"])
    r = await client.get(f"/customers/{c['id']}/contacts?tag=premium")
    data = r.json()
    assert len(data) == 1
    assert data[0]["email"] == "premium@example.com"


@pytest.mark.asyncio
async def test_list_contacts_excludes_unsubscribed_by_default(client: AsyncClient):
    c = await create_customer(client)
    contact = await add_contact(client, c["id"], email="unsub@example.com")
    await client.patch(f"/customers/{c['id']}/contacts/{contact['id']}/unsubscribe")
    r = await client.get(f"/customers/{c['id']}/contacts")
    assert all(ct["email"] != "unsub@example.com" for ct in r.json())


@pytest.mark.asyncio
async def test_list_contacts_include_unsubscribed(client: AsyncClient):
    c = await create_customer(client)
    contact = await add_contact(client, c["id"], email="unsub@example.com")
    await client.patch(f"/customers/{c['id']}/contacts/{contact['id']}/unsubscribe")
    r = await client.get(f"/customers/{c['id']}/contacts?subscribed_only=false")
    assert any(ct["email"] == "unsub@example.com" for ct in r.json())


# ── PATCH /customers/{id}/contacts/{cid} ──────────────────────────────────────

@pytest.mark.asyncio
async def test_update_contact_name(client: AsyncClient):
    c = await create_customer(client)
    contact = await add_contact(client, c["id"])
    r = await client.patch(
        f"/customers/{c['id']}/contacts/{contact['id']}",
        json={"first_name": "Alicia"}
    )
    assert r.status_code == 200
    assert r.json()["first_name"] == "Alicia"


@pytest.mark.asyncio
async def test_update_contact_tags(client: AsyncClient):
    c = await create_customer(client)
    contact = await add_contact(client, c["id"], tags=["old"])
    r = await client.patch(
        f"/customers/{c['id']}/contacts/{contact['id']}",
        json={"tags": ["new1", "new2"]}
    )
    assert r.status_code == 200
    assert r.json()["tags"] == ["new1", "new2"]


@pytest.mark.asyncio
async def test_update_contact_duplicate_email_conflict(client: AsyncClient):
    c = await create_customer(client)
    await add_contact(client, c["id"], email="taken@example.com")
    other = await add_contact(client, c["id"], email="other@example.com")
    r = await client.patch(
        f"/customers/{c['id']}/contacts/{other['id']}",
        json={"email": "taken@example.com"}
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_update_contact_not_found(client: AsyncClient):
    c = await create_customer(client)
    r = await client.patch(f"/customers/{c['id']}/contacts/ghost", json={"first_name": "X"})
    assert r.status_code == 404


# ── DELETE /customers/{id}/contacts/{cid} ─────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_contact_success(client: AsyncClient):
    c = await create_customer(client)
    contact = await add_contact(client, c["id"])
    r = await client.delete(f"/customers/{c['id']}/contacts/{contact['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True


@pytest.mark.asyncio
async def test_delete_contact_not_found(client: AsyncClient):
    c = await create_customer(client)
    r = await client.delete(f"/customers/{c['id']}/contacts/ghost")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_contact_removes_from_list(client: AsyncClient):
    c = await create_customer(client)
    contact = await add_contact(client, c["id"])
    await client.delete(f"/customers/{c['id']}/contacts/{contact['id']}")
    r = await client.get(f"/customers/{c['id']}/contacts")
    assert r.json() == []


# ── PATCH /customers/{id}/contacts/{cid}/unsubscribe ─────────────────────────

@pytest.mark.asyncio
async def test_unsubscribe_contact(client: AsyncClient):
    c = await create_customer(client)
    contact = await add_contact(client, c["id"])
    r = await client.patch(f"/customers/{c['id']}/contacts/{contact['id']}/unsubscribe")
    assert r.status_code == 200
    assert r.json()["is_subscribed"] is False


@pytest.mark.asyncio
async def test_unsubscribe_contact_not_found(client: AsyncClient):
    c = await create_customer(client)
    r = await client.patch(f"/customers/{c['id']}/contacts/ghost/unsubscribe")
    assert r.status_code == 404


# ── POST /customers/{id}/segments ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_segment_success(client: AsyncClient):
    c = await create_customer(client)
    r = await client.post(f"/customers/{c['id']}/segments", json={"name": "VIP"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "VIP"
    assert "id" in data


@pytest.mark.asyncio
async def test_create_segment_customer_not_found(client: AsyncClient):
    r = await client.post("/customers/ghost/segments", json={"name": "VIP"})
    assert r.status_code == 404


# ── GET /customers/{id}/segments ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_segments_empty(client: AsyncClient):
    c = await create_customer(client)
    r = await client.get(f"/customers/{c['id']}/segments")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_segments_includes_contact_count(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "News"})
    seg_id = seg_r.json()["id"]
    contact = await add_contact(client, c["id"])
    await client.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [contact["id"]]}
    )
    r = await client.get(f"/customers/{c['id']}/segments")
    assert r.json()[0]["contact_count"] == 1


# ── GET /customers/{id}/segments/{sid} ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_segment_with_contacts(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "Alpha"})
    seg_id = seg_r.json()["id"]
    contact = await add_contact(client, c["id"])
    await client.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [contact["id"]]}
    )
    r = await client.get(f"/customers/{c['id']}/segments/{seg_id}")
    assert r.status_code == 200
    data = r.json()
    assert len(data["contacts"]) == 1
    assert data["contacts"][0]["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_segment_not_found(client: AsyncClient):
    c = await create_customer(client)
    r = await client.get(f"/customers/{c['id']}/segments/ghost")
    assert r.status_code == 404


# ── PATCH /customers/{id}/segments/{sid} ─────────────────────────────────────

@pytest.mark.asyncio
async def test_rename_segment(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "Old"})
    seg_id = seg_r.json()["id"]
    r = await client.patch(f"/customers/{c['id']}/segments/{seg_id}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"


# ── DELETE /customers/{id}/segments/{sid} ─────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_segment_success(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "ToDelete"})
    seg_id = seg_r.json()["id"]
    r = await client.delete(f"/customers/{c['id']}/segments/{seg_id}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True


@pytest.mark.asyncio
async def test_delete_segment_preserves_contacts(client: AsyncClient):
    """Deleting a segment must not delete the contacts inside it."""
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "G"})
    seg_id = seg_r.json()["id"]
    contact = await add_contact(client, c["id"])
    await client.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [contact["id"]]}
    )
    await client.delete(f"/customers/{c['id']}/segments/{seg_id}")
    r = await client.get(f"/customers/{c['id']}/contacts")
    assert len(r.json()) == 1


# ── POST /customers/{id}/segments/{sid}/contacts ──────────────────────────────

@pytest.mark.asyncio
async def test_add_contacts_to_segment(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "S"})
    seg_id = seg_r.json()["id"]
    c1 = await add_contact(client, c["id"], email="one@example.com")
    c2 = await add_contact(client, c["id"], email="two@example.com")
    r = await client.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [c1["id"], c2["id"]]}
    )
    assert r.status_code == 200
    assert r.json()["added_count"] == 2


@pytest.mark.asyncio
async def test_add_contacts_silently_skips_duplicates(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "S"})
    seg_id = seg_r.json()["id"]
    contact = await add_contact(client, c["id"])
    # Add once
    await client.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [contact["id"]]}
    )
    # Add again — should not raise, should report 0 added
    r = await client.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [contact["id"]]}
    )
    assert r.status_code == 200
    assert r.json()["added_count"] == 0


@pytest.mark.asyncio
async def test_add_contacts_silently_skips_wrong_customer(client: AsyncClient):
    c1 = await create_customer(client, "Corp A")
    c2 = await create_customer(client, "Corp B")
    seg_r = await client.post(f"/customers/{c1['id']}/segments", json={"name": "S"})
    seg_id = seg_r.json()["id"]
    other_contact = await add_contact(client, c2["id"], email="other@example.com")
    r = await client.post(
        f"/customers/{c1['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [other_contact["id"]]}
    )
    assert r.status_code == 200
    assert r.json()["added_count"] == 0


# ── DELETE /customers/{id}/segments/{sid}/contacts/{cid} ─────────────────────

@pytest.mark.asyncio
async def test_remove_contact_from_segment(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "S"})
    seg_id = seg_r.json()["id"]
    contact = await add_contact(client, c["id"])
    await client.post(
        f"/customers/{c['id']}/segments/{seg_id}/contacts",
        json={"contact_ids": [contact["id"]]}
    )
    r = await client.delete(
        f"/customers/{c['id']}/segments/{seg_id}/contacts/{contact['id']}"
    )
    assert r.status_code == 200
    assert r.json()["removed"] is True


@pytest.mark.asyncio
async def test_remove_contact_not_in_segment(client: AsyncClient):
    c = await create_customer(client)
    seg_r = await client.post(f"/customers/{c['id']}/segments", json={"name": "S"})
    seg_id = seg_r.json()["id"]
    contact = await add_contact(client, c["id"])
    r = await client.delete(
        f"/customers/{c['id']}/segments/{seg_id}/contacts/{contact['id']}"
    )
    assert r.status_code == 404
