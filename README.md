# Multi-Tenant Email Service — FastAPI + Resend

Send emails on behalf of multiple customers, each from their own verified domain,
through a single Resend account.

---

## Quick Start (Local)

### 1. Clone and install

```bash
cd resend-fastapi
python -m venv venv

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Set up environment

```bash
copy .env.example .env    # Windows
# cp .env.example .env    # Mac/Linux
```

Open `.env` and add your Resend API key:
```
RESEND_API_KEY=re_your_actual_key_here
```

Leave `DATABASE_URL` as-is for local dev (SQLite — no extra setup needed).

### 3. Run the server

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs — you'll see the full Swagger UI.

---

## How to Test End-to-End

Follow these steps in order using the Swagger UI at `/docs`.

### Step 1 — Create a customer
```
POST /customers/
{"name": "Acme Corp"}
```
Copy the `id` from the response.

### Step 2 — Add and verify their domain
```
POST /customers/{id}/domains
{"domain_name": "acme.com"}
```
This returns DNS records. Give them to your customer to add to their registrar.

Then trigger verification:
```
POST /customers/{id}/domains/verify
```
Poll the status until verified:
```
GET /customers/{id}/domains/status
```

### Step 3 — Add contacts
```
POST /customers/{id}/contacts
{"email": "john@example.com", "first_name": "John", "tags": ["newsletter"]}
```

### Step 4 — Send email
```
POST /customers/{id}/send
{
  "subject": "Hello {{first_name}}!",
  "html": "<p>Hi {{first_name}}, welcome to our newsletter.</p>"
}
```

---

## Webhook Setup (Local Testing)

Install the Svix CLI to forward Resend webhooks to your local server:

```bash
npm install -g svix
npx svix-cli listen http://localhost:8000/webhooks/resend
```

It gives you a public URL like `https://play.svix.com/in/...` — register that in
Resend dashboard → Webhooks → Add Endpoint.

Copy the signing secret into `.env` as `WEBHOOK_SECRET`.

---

## Running the Test Suite

### Install dev dependencies

```bash
pip install -r requirements-dev.txt
```

### Unit + integration tests (fast, no real Resend key needed)

All Resend API calls are mocked. Tests use an in-memory SQLite database —
nothing is written to disk, nothing is sent over the network.

```bash
pytest
```

You should see output like:
```
collected 70+ items
tests/test_customers.py  ............
tests/test_contacts.py   ......................
tests/test_email.py      ..............
tests/test_campaigns.py  ..........
tests/test_webhooks.py   .........
```

### Playwright end-to-end tests (live server)

The e2e tests start a real uvicorn server on port 8001 and hit it via
actual HTTP — same path a real client would take. Resend calls are still
mocked so no API key is needed.

First install the Playwright browser engine (only needed once):
```bash
playwright install chromium
```

Then run the e2e suite:
```bash
pytest tests/e2e/ -v
```

### Run everything together

```bash
# Unit tests
pytest

# E2E tests
pytest tests/e2e/ -v
```

---

## Project Structure

```
resend-fastapi/
├── app/
│   ├── main.py               # App entry point, router registration
│   ├── models.py             # SQLAlchemy models (Customer, Contact, Segment, Campaign)
│   ├── database.py           # DB engine, session, init
│   ├── routers/
│   │   ├── customers.py      # Customer CRUD + domain setup
│   │   ├── contacts.py       # Contact + segment management
│   │   ├── email.py          # Send email routes
│   │   ├── campaigns.py      # Campaign history CRUD
│   │   └── webhooks.py       # Resend delivery event handler
│   └── services/
│       └── resend_service.py # ONLY file that calls Resend API
├── tests/
│   ├── conftest.py           # Async DB + mock fixtures for unit tests
│   ├── test_customers.py
│   ├── test_contacts.py
│   ├── test_email.py
│   ├── test_campaigns.py
│   ├── test_webhooks.py
│   └── e2e/
│       ├── conftest.py       # Live uvicorn server fixture
│       └── test_api_e2e.py   # Playwright API tests
├── .env.example
├── .gitignore
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt
├── SECURITY.md
└── README.md
```

---

## Key Design Decisions

**Why not use Resend Audiences/Broadcasts?**
Resend's Segments feature (which enables filtering like "send to premium users only")
is available in the dashboard UI only — there is no API for segment-based filtering.
For any real production use where you need contact segmentation, you must own the
contact layer yourself.

**Why one Resend account for all customers?**
Resend supports multiple verified domains under one account. Each customer gets their
own domain (so emails come from `hello@acme.com`, `hello@betacorp.com`, etc.) but
all sending goes through your single API key.

**Why auto-unsubscribe on bounce?**
Resend monitors your account's bounce rate. High bounce rates lead to account
suspension. The webhook handler marks bounced/complained contacts as
`is_subscribed = False` to protect sending reputation.

**How are retries made safe?**
`/send` accepts an `Idempotency-Key` header and tracks per-recipient send
status, so a retried request never re-emails a contact who already received
the campaign. See [docs/RETRY_SAFETY.md](docs/RETRY_SAFETY.md) for the full
design, including the known limitations that are deliberately not fixed yet.

---

## Security Notes

See [SECURITY.md](SECURITY.md) for the full security review. Key points for production:

- **Add authentication.** All endpoints are currently unauthenticated. Add an `X-API-Key` header check or JWT middleware before exposing this to the internet.
- **Set `WEBHOOK_SECRET`** in `.env`. Without it, anyone can POST to `/webhooks/resend` and your handler will process it.
- **Set `DEBUG=false`** (the default). `DEBUG=true` logs all SQL queries including contact email addresses to stdout.
- **Add rate limiting** on the `/send` endpoints to prevent accidental or malicious bulk sending.
- **Never commit `.env`** — it's in `.gitignore` but double-check before pushing.

---

## Switching to PostgreSQL (Production)

In `.env`, replace the DATABASE_URL:
```
DATABASE_URL=postgresql+asyncpg://user:password@localhost/resend_db
```

Then install the async PostgreSQL driver if not already present:
```bash
pip install asyncpg
```

Tables are created automatically on startup — no migrations needed for initial setup.
For schema changes in production, use Alembic.
