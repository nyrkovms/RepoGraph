"""Resolver reference cases (SPEC §14.2, IMPL D4): the same (package, version) set as the native tool.

Each case of tests/reference/<ecosystem>/ is resolved through the API service in a rolled-back transaction, on
recorded upstream responses; see tests/reference/README.md for the format."""

import asyncio
import json
import uuid
from pathlib import Path

import pytest
import yaml
from packaging.utils import canonicalize_name
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.ecosystems.registry import load_builtin_plugins
from app.fetcher import pipeline
from app.models.enums import Ecosystem, RegistryProtocol
from app.models.registry import Registry
from app.models.source_profile import SourceProfile, SourceProfileRegistry
from app.resolver.service import resolve_graph
from app.schemas.resolve import GraphResolveRequest

load_builtin_plugins()
CASES = sorted(p.parent for p in (Path(__file__).resolve().parents[1] / "reference").glob("*/*/case.yaml"))


async def _profile(db: AsyncSession, ecosystem: str, sources: list[dict]) -> str:
    """The case's sources in order; `pypi` is the catalog's source, the others are created for the case."""
    members = []
    for source in sources:
        if source["slug"] == "pypi":
            members.append(await pipeline.get_or_create_pypi_registry(db))
            continue
        registry = await db.scalar(select(Registry).where(Registry.slug == source["slug"]))
        if registry is None:
            registry = Registry(ecosystem=Ecosystem(ecosystem), slug=source["slug"], name=source["slug"],
                                base_url=source["base_url"], protocol=RegistryProtocol(source["protocol"]),
                                priority=source.get("priority", 50), routing=source.get("routing"), enabled=True)
            db.add(registry)
            await db.flush()
        members.append(registry)
    slug = f"ref-{uuid.uuid4().hex[:8]}"
    profile = SourceProfile(ecosystem=Ecosystem(ecosystem), slug=slug, name=slug, is_default=False)
    db.add(profile)
    await db.flush()
    for position, registry in enumerate(members):
        db.add(SourceProfileRegistry(profile_id=profile.id, registry_id=registry.id, ecosystem=Ecosystem(ecosystem),
                                     position=position))
    await db.commit()
    return slug


@pytest.mark.parametrize("case_dir", CASES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_reference_case(case_dir: Path) -> None:
    expected_file = case_dir / "expected.json"
    if not expected_file.exists():
        pytest.skip("no expected.json yet: scripts/generate_resolver_references.sh")
    case = yaml.safe_load((case_dir / "case.yaml").read_text())
    expected = json.loads(expected_file.read_text())
    ecosystem = case_dir.parent.name

    async def run() -> set[tuple[str, str]]:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                profile = await _profile(db, ecosystem, case["sources"])
                graph = await resolve_graph(db, GraphResolveRequest(
                    ecosystem=ecosystem, profile=profile, roots=case["roots"], environment=case["environment"],
                    options={"timeout_s": 120},
                ))
                assert graph.error is None, graph.error
                return {(canonicalize_name(node.name), node.version) for node in graph.nodes}
            finally:
                await db.close()
                await transaction.rollback()
                await engine.dispose()

    ours = asyncio.run(run())
    native = {(canonicalize_name(item["name"]), item["version"]) for item in expected["install"]}
    allowed = {canonicalize_name(d["package"]) for d in case.get("allowed_differences") or []}
    ours_cmp = {pair for pair in ours if pair[0] not in allowed}
    native_cmp = {pair for pair in native if pair[0] not in allowed}
    matched = len(ours_cmp & native_cmp) / max(len(native_cmp), 1)
    assert ours_cmp == native_cmp, (
        f"{matched:.0%} of {expected['tool']}'s answer; only ours: {sorted(ours_cmp - native_cmp)}; "
        f"only {expected['tool']}: {sorted(native_cmp - ours_cmp)}"
    )
