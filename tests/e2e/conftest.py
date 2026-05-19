"""
Playwright e2e conftest — spins up a real uvicorn server for the session.

Architecture:
  - A temporary SQLite file is used (not in-memory) because the server runs
    in a background thread and cannot share an async in-memory session.
  - All resend_service functions are patched at import time before uvicorn
    starts, so no real Resend API key is ever required.
  - Playwright's sync APIRequestContext is used to drive real HTTP requests
    against http://127.0.0.1:8001 — the same URL a browser or real client
    would call.

Why Playwright on top of the pytest+httpx tests?
  The httpx tests inject the ASGI app directly and bypass the actual HTTP
  stack. Playwright tests exercise the full path: socket → uvicorn →
  FastAPI middleware → router → DB. This catches issues like missing CORS
  headers, middleware ordering bugs, or serialisation differences that only
  appear over real TCP.
"""
import os
import time
import threading
import tempfile
import pytest
import uvicorn
from unittest.mock import patch, MagicMock
from playwright.sync_api import sync_playwright, Playwright, APIRequestContext


E2E_PORT = 8001
E2E_BASE_URL = f"http://127.0.0.1:{E2E_PORT}"
E2E_DB_FILE = os.path.join(tempfile.gettempdir(), "resend_e2e_test.db")

# Patch values used throughout the session
_MOCK_ADD_DOMAIN    = MagicMock(return_value={"id": "dom_e2e", "status": "not_started", "records": []})
_MOCK_VERIFY_DOMAIN = MagicMock(return_value={"id": "dom_e2e"})
_MOCK_GET_STATUS    = MagicMock(return_value={"status": "verified"})
_MOCK_SEND_SINGLE   = MagicMock(return_value={"id": "email_e2e_001"})
_MOCK_SEND_BULK     = MagicMock(return_value=[{"data": [{"id": "b1"}]}])


def _start_server():
    """
    Apply all Resend mocks, point the DB at a temp file, then start uvicorn.
    Runs in a daemon thread — dies automatically when the test process exits.
    """
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{E2E_DB_FILE}"
    os.environ["RESEND_API_KEY"] = "re_test_placeholder"

    with (
        patch("app.routers.customers.resend_service.add_domain",    _MOCK_ADD_DOMAIN),
        patch("app.routers.customers.resend_service.verify_domain", _MOCK_VERIFY_DOMAIN),
        patch("app.routers.customers.resend_service.get_domain_status", _MOCK_GET_STATUS),
        patch("app.routers.email.send_single", _MOCK_SEND_SINGLE),
        patch("app.routers.email.send_bulk",   _MOCK_SEND_BULK),
    ):
        # Import app AFTER patching so mocks are in place
        from app.main import app  # noqa: PLC0415
        config = uvicorn.Config(app, host="127.0.0.1", port=E2E_PORT, log_level="error")
        server = uvicorn.Server(config)
        server.run()


@pytest.fixture(scope="session")
def live_server():
    """Start the uvicorn server once for the whole e2e session."""
    # Remove any leftover test DB from a previous run
    if os.path.exists(E2E_DB_FILE):
        os.remove(E2E_DB_FILE)

    thread = threading.Thread(target=_start_server, daemon=True)
    thread.start()

    # Wait until the server is ready (up to 10 s)
    import httpx
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{E2E_BASE_URL}/")
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("E2E server did not start within 10 seconds")

    yield E2E_BASE_URL

    # Cleanup temp DB
    if os.path.exists(E2E_DB_FILE):
        os.remove(E2E_DB_FILE)


@pytest.fixture(scope="session")
def playwright_instance():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="function")
def api(playwright_instance: Playwright, live_server: str) -> APIRequestContext:
    """
    Returns a Playwright APIRequestContext scoped to one test function.
    Each test gets a fresh context with no shared cookies or state.
    """
    ctx = playwright_instance.request.new_context(base_url=live_server)
    yield ctx
    ctx.dispose()
