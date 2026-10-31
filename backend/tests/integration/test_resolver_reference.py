"""Resolver reference cases (SPEC §14.2, IMPL D4): the same (package, version) set as the native tool.

Each case of tests/reference/<ecosystem>/ is resolved through the API service in a rolled-back transaction, on
recorded upstream responses; see tests/reference/README.md for the format."""

import asyncio
import json
from collections.abc import Callable
import os
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
from app.schemas.resolve import GraphOptions, GraphResolveRequest

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
            registry = Registry(ecosystem=Ecosystem(ecosystem), slug=source["slug"], name=source["slug"])
            db.add(registry)
        # The case's own configuration, also over a source of that slug left in the database.
        registry.base_url, registry.protocol = source["base_url"], RegistryProtocol(source["protocol"])
        registry.priority, registry.routing, registry.enabled = source.get("priority", 50), source.get("routing"), True
        if "config" in source:   # RPM: namespace, arch, releasever (§6.4); other cases keep the source's own
            registry.config = source["config"]
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
    if ecosystem == "rpm" and not os.environ.get("RPM_REFERENCES"):
        # Their repositories are frozen vault snapshots of tens of MB: too big for offline fixtures (IMPL E6), so the
        # nightly workflow runs them against the live vault with RPM_REFERENCES=1.
        pytest.skip("RPM references run in the nightly workflow (RPM_REFERENCES=1)")
    # PyPI names compare normalized (PEP 503), NuGet ids case-insensitively; elsewhere the spelling is the identity
    # (Go paths, crates names).
    name = {"pypi": canonicalize_name, "nuget": str.lower}.get(ecosystem, str)
    allowed = {name(d["package"]) for d in case.get("allowed_differences") or []}

    async def run(environment: dict) -> set[tuple[str, str]]:
        engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
            try:
                profile = await _profile(db, ecosystem, case["sources"])
                graph = await resolve_graph(db, GraphResolveRequest(
                    ecosystem=ecosystem, profile=profile, roots=case["roots"], environment=environment,
                    managed=case.get("managed") or [],
                    # Recording waits for the crates.io API (1 request/s) once per crate the case reaches: longer
                    # than the API allows a request to take.
                    options=GraphOptions.model_construct(timeout_s=900) if os.environ.get("HTTP_RECORD_DIR")
                    else GraphOptions(timeout_s=120),
                ))
                # A restore the native tool fails (NuGet NU1605) still has its packages; the error code must agree.
                wanted = expected.get("errors") or []
                if wanted:
                    assert graph.error is not None and graph.error["code"] in wanted, graph.error
                else:
                    assert graph.error is None, graph.error
                errors = {node.name: node.errors for node in graph.nodes if node.errors}
                assert not {n for n in errors if name(n) not in allowed}, errors
                return {(name(node.name), node.version) for node in graph.nodes}
            finally:
                await db.close()
                await transaction.rollback()
                await engine.dispose()

    ours = asyncio.run(run(case["environment"]))
    # crates: the versions for every platform as well, which is what Cargo.lock holds (SPEC §9.4).
    ours_lock = asyncio.run(run({**case["environment"], "target": "*"})) if "lockfile" in expected else None
    if os.environ.get("HTTP_RECORD_DIR"):
        # scripts/record_fixtures.sh: the upstream answers are recorded now and expected.json is generated right after.
        pytest.skip("recording upstream responses; the expectation is regenerated afterwards")
    _compare(ours, expected["install"], allowed, expected["tool"], name)
    if ours_lock is not None:
        _compare(ours_lock, expected["lockfile"], allowed, f"{expected['tool']} (Cargo.lock)", name)


def _compare(ours: set[tuple[str, str]], native_rows: list[dict], allowed: set[str], tool: str,
             name: Callable[[str], str]) -> None:
    native = {(name(item["name"]), item["version"]) for item in native_rows}
    ours_cmp = {pair for pair in ours if pair[0] not in allowed}
    native_cmp = {pair for pair in native if pair[0] not in allowed}
    matched = len(ours_cmp & native_cmp) / max(len(native_cmp), 1)
    assert ours_cmp == native_cmp, (
        f"{matched:.0%} of {tool}'s answer; only ours: {sorted(ours_cmp - native_cmp)}; "
        f"only {tool}: {sorted(native_cmp - ours_cmp)}"
    )
