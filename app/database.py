import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from dotenv import load_dotenv
from .models import Base

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./resend_local.db")

# echo=True logs every SQL statement — only enable in local dev (DEBUG=true)
# WARNING: echo=True will print contact emails and names to stdout/logs in production.
_SQL_ECHO = os.getenv("DEBUG", "false").lower() == "true"
engine = create_async_engine(DATABASE_URL, echo=_SQL_ECHO)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


async def init_db():
    """Create all tables if they don't exist. Called on app startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI dependency — yields a DB session per request."""
    async with AsyncSessionLocal() as session:
        yield session
