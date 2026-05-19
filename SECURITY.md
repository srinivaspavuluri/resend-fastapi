# Security Review — resend-fastapi

Review date: 2026-05-19  
Scope: full codebase (`app/`, configuration files)

---

## Findings Summary

| # | Severity | Area | Finding | Fixed in code? |
|---|----------|------|---------|----------------|
| 1 | 🔴 Critical | Auth | No authentication — all endpoints are publicly accessible | No (requires project decision) |
| 2 | 🔴 Critical | Secrets | `echo=True` in DB engine logs all SQL (including data values) to stdout | ✅ Fixed |
| 3 | 🟠 High | Secrets | `WEBHOOK_SECRET` is read at module import time — runtime changes ignored; also silently skipped if unset in production | ✅ Hardened with warning |
| 4 | 🟠 High | Input | No max-length on `html` / `subject` request fields — allows very large payloads | ✅ Fixed |
| 5 | 🟡 Medium | Config | No `.gitignore` — `resend_local.db`, `venv/`, and `.env` can be committed | ✅ Added `.gitignore` |
| 6 | 🟡 Medium | Config | No `.env.example` present despite being referenced in README | ✅ Added |
| 7 | 🟡 Medium | Network | No CORS policy set — browser clients get no explicit headers | No (add if serving a frontend) |
| 8 | 🟡 Medium | Network | No rate limiting — any caller can spam all endpoints | No (add middleware in production) |
| 9 | 🟢 Low | Logging | Uvicorn access logs include customer/contact IDs in URLs | Informational only |
| 10 | 🟢 Low | DB | `resend_local.db` written to the project root — risk of committing | ✅ Covered by `.gitignore` |

---

## Detailed Findings

### 1 🔴 No Authentication (Critical)

**What:** Every endpoint (`/customers/`, `/contacts/`, `/send/`, etc.) is wide open — no API key, JWT, or session token is required. Any client that can reach the server can list all customers, read all contacts, delete campaigns, or send emails.

**Impact:** In production, this means any internet user can exfiltrate your entire customer and contact database, or trigger email sends that cost real money.

**Fix options (choose one):**
- **Simple:** Add a shared `X-API-Key` header check via FastAPI dependency.
- **Proper:** Add per-customer bearer tokens with a `tokens` table. Each customer gets their own key and can only access their own data.
- **Full:** Integrate an identity provider (Auth0, Supabase Auth, Clerk) and issue JWTs.

Minimal example (shared key — suitable for internal services):
```python
# app/auth.py
import os
from fastapi import Header, HTTPException

API_KEY = os.getenv("API_KEY", "")

async def require_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
```
Then add `dependencies=[Depends(require_api_key)]` to each router.

---

### 2 🔴 SQL Logging in Production (Critical)

**What:** `database.py` sets `echo=True` on the SQLAlchemy engine. This prints every SQL statement — including `INSERT` values containing email addresses, names, and tags — to stdout/logs.

**Fix:** Use `echo=True` only when `DEBUG=true` is set in the environment.  
✅ Applied to `app/database.py`.

---

### 3 🟠 Webhook Secret Handling (High)

**What:**
- `WEBHOOK_SECRET` is read at module import time (`WEBHOOK_SECRET = os.getenv(...)`). If the env var is set after the module loads, it has no effect.
- If the secret is absent in production, verification is silently skipped with no warning.

**Fix:** Read the secret per-request (from `os.getenv`) so it always reflects the current environment. Emit a startup warning if no secret is configured.  
✅ Applied to `app/routers/webhooks.py`.

---

### 4 🟠 No Input Length Limits (High)

**What:** The `html` field on `SendEmailRequest` and `SendSingleRequest` has no max length. A client could POST a 50 MB HTML body, consuming memory and potentially causing OOM under load.

**Fix:** Add `max_length` constraints in Pydantic fields.  
✅ Applied to `app/routers/email.py`.

---

### 5 🟡 Missing `.gitignore` (Medium)

**What:** No `.gitignore` means `venv/`, `resend_local.db`, `__pycache__/`, and `.env` (containing the real Resend API key) can be accidentally committed.

**Fix:** Added `.gitignore` to the project root.

---

### 6 🟡 Missing `.env.example` (Medium)

**What:** README references `copy .env.example .env` but the file didn't exist — a new developer following the README would get an error.

**Fix:** Added `.env.example` with safe placeholder values.

---

### 7 🟡 No CORS Policy (Medium)

**What:** FastAPI returns no `Access-Control-Allow-*` headers. If you ever serve a browser-based frontend from a different origin, all requests will be blocked by the browser's same-origin policy.

**Recommended fix (add to `app/main.py`):**
```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://yourdashboard.com"],  # never use ["*"] in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```
Not applied automatically — origins are specific to your deployment.

---

### 8 🟡 No Rate Limiting (Medium)

**What:** There is no rate limiting on any endpoint. The `/send` endpoint in particular could be abused to trigger many Resend API calls, costing real money.

**Recommended fix:** Add `slowapi` (a FastAPI-compatible rate limiter):
```bash
pip install slowapi
```
```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@router.post("/{customer_id}/send")
@limiter.limit("10/minute")
async def send_email(request: Request, ...):
    ...
```
Not applied — rate limits are deployment-specific.

---

## What's Done Well

- **UUIDs for all IDs** — customer, contact, segment, and campaign IDs are UUIDs (not sequential integers), making enumeration attacks much harder.
- **ORM throughout** — all database access goes through SQLAlchemy ORM. No raw string SQL, so SQL injection risk is minimal.
- **Webhook signature verification** — Svix HMAC-SHA256 signature checking is implemented correctly (compare_digest to avoid timing attacks).
- **Unsubscribe on bounce/complaint** — protects Resend sender reputation automatically.
- **Pydantic v2 validation** — all request bodies are validated before reaching business logic; invalid email formats, missing required fields, and wrong types are rejected automatically.
- **No secrets in source code** — all keys loaded from environment variables / `.env`.
