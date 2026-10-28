from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.async_db_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def loop_local_session() -> AsyncGenerator[AsyncSession, None]:
    """Session for code that runs its own event loop (asyncio.run inside the resolver thread): pooled asyncpg
    connections are bound to the loop that opened them and must not be reused from another one."""
    loop_engine = create_async_engine(settings.async_db_url, poolclass=NullPool)
    try:
        async with AsyncSession(loop_engine, expire_on_commit=False) as session:
            yield session
    finally:
        await loop_engine.dispose()


@asynccontextmanager
async def loop_local_sessionmaker(pool_size: int = 5) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Pooled sessions for one event loop, e.g. a task that updates many packages concurrently inside asyncio.run:
    the pool is created and disposed in that loop. The overflow leaves room for the breaker's side transaction,
    opened while every worker still holds its own connection."""
    loop_engine = create_async_engine(settings.async_db_url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True)
    try:
        yield async_sessionmaker(bind=loop_engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await loop_engine.dispose()
