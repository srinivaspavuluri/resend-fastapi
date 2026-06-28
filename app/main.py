import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .database import init_db
from .routers import customers, contacts, email, webhooks, campaigns

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create DB tables on startup (no migrations needed for local dev)."""
    await init_db()
    yield


app = FastAPI(
    title="Multi-Tenant Email Service — Resend + FastAPI",
    description=(
        "A FastAPI backend that lets multiple customers send emails "
        "from their own verified domains, through a single Resend account."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(customers.router)
app.include_router(contacts.router)
app.include_router(email.router)
app.include_router(campaigns.router)
app.include_router(webhooks.router)


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "message": "Resend + FastAPI service is running"}
