# Retry Safety & Idempotency

How `POST /customers/{id}/send` protects against double-emailing a contact when
a request is retried, and the limitations that are still open. This document
exists so a future maintainer (including future-me) doesn't have to re-derive
this from the diffs.

---

## The failure this protects against

`/send` batches recipients into groups of 100 and calls Resend's batch
endpoint once per group. If a batch call fails partway through a send (rate
limit, network error, Resend outage), the contacts in batches that already
succeeded have been emailed; the rest have not. A naive retry of the same
request re-sends to everyone, including the contacts who already received it.

Two independent mechanisms close this, operating at two different layers.

---

## Layer 1 — request-level idempotency (`Idempotency-Key` header)

The caller supplies an `Idempotency-Key` header. It becomes the `Campaign.id`:

```python
campaign_id = idempotency_key or new_id()
```

**This must be caller-supplied, not server-generated.** A server-generated key
changes on every request object, including retries of the same logical
request — which defeats the entire point. The key has to identify the
*request*, and survive across the caller's own retry of it.

Before doing any contact lookup, the endpoint checks whether a campaign with
this ID already exists:

| Existing campaign status | Response |
|---|---|
| `sent` | Cached success response returned. No send happens. |
| `partial` / `sending` | `409` with `campaign_id`, `campaign_status`, `sent_to`, and instructions to use `resume_from_campaign_id`. |
| (none) | Proceeds to send normally. |

This is what makes a retry of an already-completed request safe — but it does
**not**, by itself, make recovering from a *partial* failure safe. That's
layer 2.

---

## Layer 2 — per-recipient tracking (`CampaignRecipient` + resume)

Every contact who is actually handed to Resend gets a row the moment the
batch call returns an ID for them:

```python
class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    id = Column(String, primary_key=True, default=new_id)
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    contact_id = Column(String, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    resend_email_id = Column(String(255), nullable=True, index=True)
    status = Column(String(50), default="queued")  # queued | sent | delivered | bounced | complained
    updated_at = Column(DateTime, default=datetime.utcnow)
```

`status` is advanced by the webhook handler (`app/routers/webhooks.py`) as
Resend's delivery events arrive — this table doubles as delivery-status
tracking, not just a resume log.

To recover from a `partial` campaign, the caller passes
`resume_from_campaign_id` (a *different* request, with its own fresh
`Idempotency-Key`):

```python
already_sent_subq = select(CampaignRecipient.contact_id).where(
    and_(
        CampaignRecipient.campaign_id == body.resume_from_campaign_id,
        CampaignRecipient.contact_id.isnot(None),
    )
)
query = query.where(Contact.id.notin_(already_sent_subq))
```

Two non-obvious things in that block, both worth preserving as comments if
this code ever gets refactored:

1. **`.isnot(None)` is load-bearing, not defensive boilerplate.** `contact_id`
   is nullable (`ondelete="SET NULL"`). SQL's `NOT IN` uses three-valued
   logic — if the subquery returns even one `NULL`, the whole `NOT IN`
   predicate evaluates to unknown for *every* row, not just the row tied to
   the `NULL`. Without this filter, one deleted contact between the failed
   send and the resume silently zeroes out the entire exclusion list, and
   resume re-emails everyone.
2. **Resume is restricted to `status == "partial"`, not `!= "sent"`.**
   Allowing resume from `"sending"` would race an in-flight send — two
   processes building exclusion sets against a table still being written to.

---

## Known limitation: per-chunk idempotency keys are currently inert

Each chunk inside the send loop is given its own key:

```python
chunk_key = f"{campaign_id}/batch-{i // 100}"
chunk_results = await send_batch(..., idempotency_key=chunk_key)
```

**No code path in this service currently calls `send_batch` twice with the
same `chunk_key`.** The layer-1 check short-circuits before the loop runs
again for an existing `campaign_id`; `resume_from_campaign_id` always mints a
fresh `campaign_id` and therefore fresh chunk keys. Resend's own dedup on
these keys is real and would work if exercised — it is just never exercised
by anything in this codebase today.

This is not actively harmful, but don't read its presence as proof that
chunk-level replay is handled — it isn't, by anything. If a future change
causes `campaign_id` to be reused across send attempts, re-verify this
section before assuming the chunk keys cover it.

---

## Known limitation: the timeout-after-accept window

If the `httpx` call to `/emails/batch` times out *after* Resend has accepted
and begun sending the batch, but before the response reaches this service,
the exception handler marks the campaign `partial` — but no
`CampaignRecipient` rows exist for that chunk, since the response (containing
the email IDs) never arrived. A subsequent resume cannot exclude contacts it
has no record of, and would re-email that chunk.

**Mitigation in place (not a fix):** a log line fires immediately before the
`send_batch` call, recording `campaign_id`, `chunk_key`, and the specific
`contact_ids` in that chunk:

```python
logger.info(
    "send_batch attempt campaign_id=%s chunk_key=%s contact_ids=%s",
    campaign_id, chunk_key, [c.id for c in chunk_contacts],
)
```

Contact IDs are logged, not just a count — a count cannot be reconstructed
back into "which specific people" later if the contacts table has changed
since the incident. If a duplicate-send complaint surfaces, grep for the
relevant `chunk_key` to get the exact contact IDs that need manual review.

**This log line only has value if it's actually emitting.** It depends on
`logging.basicConfig(level=logging.INFO, ...)` being called somewhere on
startup (currently in `app/main.py`). If that call is ever removed or the
level raised, this entire mitigation silently stops working with no error of
any kind — Python's root logger defaults to `WARNING` and drops `INFO` calls
without complaint. Verify log output actually appears in stdout after any
change near startup configuration.

**Proper fix, not yet built:** a `CampaignChunk` table — one row per chunk
*attempt*, written before the Resend call so the attempt is on record even if
the response is lost, with status tracked independently of whether
`CampaignRecipient` rows exist. Deferred as a v1 boundary: this closes a race
that requires a network failure in a narrow window between Resend accepting
a batch and the response reaching the client, which has not been observed in
this project. The schema change and added write are real cost for a risk
that's currently theoretical at this service's volume — revisit if send
volume or failure rate increases enough to make it not theoretical.

---

## Known limitation: concurrent duplicate keys

Two requests with the same `Idempotency-Key` arriving close enough together
can both pass the "no existing campaign" check before either commits a row —
there's no row-level lock or unique-constraint race guard on that check.
Currently unhandled; would surface as a raw `500` (primary key violation on
`Campaign.id`) rather than a clean `409`. Low probability given how this
service is called today. Not fixed.

---

## Verified behavior (live, against the real Resend API)

Tested against a free Resend account using sandbox addresses
(`onboarding@resend.dev` as sender, `delivered@resend.dev` /
`bounced@resend.dev` / `complained@resend.dev` as recipients) — no verified
domain required:

- Batch response order matches submission order on `/emails/batch`, so
  `zip(chunk_contacts, chunk_results)` correctly pairs each contact with its
  own `resend_email_id`.
- `Idempotency-Key` dedup works identically on both `/emails` and
  `/emails/batch`: same key + same payload returns the identical response, no
  second email sent. Same key + different payload returns `409
  invalid_idempotent_request` from Resend.

## Test coverage

`tests/test_email.py` covers this design directly:

- `test_resume_skips_already_emailed_contacts`
- `test_resume_rejects_non_partial_statuses` (parametrized over `sent` and
  `sending`)
- `test_resume_from_nonexistent_campaign_returns_404`
- Plus 4 idempotency-specific tests covering the cached-success and `409`
  branches.
