"""The PyTorch indexes of registries.pytorch.yaml (IMPL D2): imported as shipped, the `cuda121` profile resolves torch
to a +cu121 build, whose dependencies are unknown (the cu121 index has no PEP 658 metadata, Q14). Rolled back;
upstream responses replayed."""

import asyncio
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.ecosystems.registry import load_builtin_plugins
from app.fetcher import pipeline
from app.models.registry import Registry
from app.resolver.service import resolve_graph
from app.schemas.resolve import GraphResolveRequest
from app.services import registries

load_builtin_plugins()
FILE = Path(__file__).resolve().parents[2] / "registries.pytorch.yaml"


def test_cuda121_profile_resolves_torch_to_a_cu121_build() -> None:
    async def run() -> None:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                await pipeline.get_or_create_pypi_registry(db)
                document = registries.parse_import(FILE.read_text(encoding="utf-8"))
                for item in document.registries:
                    item.enabled = None   # no live connection test; switched on below
                results = await registries.import_document(db, document)
                assert all(r.outcome != "error" for r in results), results
                await db.execute(update(Registry).where(Registry.slug.like("pytorch-%")).values(enabled=True))
                await db.commit()

                graph = await resolve_graph(db, GraphResolveRequest(roots=[{"purl": "pkg:pypi/torch"}], profile="cuda121"))
                assert graph.error is None, graph.error
                (torch,) = [n for n in graph.nodes if n.name == "torch"]
                assert torch.version.endswith("+cu121") and torch.registry == "pytorch-cu121"
                assert torch.errors == ["dependencies unknown"]
            finally:
                await db.close()
                await transaction.rollback()
                await engine.dispose()

    asyncio.run(run())
