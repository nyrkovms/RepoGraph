"""Мультиреестровый сценарий (spec fetch-metadata / registry-access):

два pypi-simple реестра (публичный и внутренний hosted) на локальном
HTTP-сервере → enumeration внутреннего, sync того же имени из публичного →
один пакет, объединение версий, артефакты помнят реестр, флаги коллизии
имени и расхождения checksum, пакет находится поиском.

Работает полностью герметично: сеть — только localhost, БД — compose postgres.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.connectors.factory import build_connector, registry_info_from_row
from app.fetcher.pipeline import run_sync_pipeline
from app.models.artifact import Artifact
from app.models.package import Package
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.version import PackageVersion
from app.tasks.enumeration import _enumerate_registry

PKG = "phub-it-collide"
# в имени wheel-файла имя пакета экранируется подчёркиваниями (PEP 427)
PKG_FILE = PKG.replace("-", "_")

JSON_CT = "application/vnd.pypi.simple.v1+json"


def project_page(base: str, sha_any: str) -> dict:
    return {
        "meta": {"api-version": "1.0"},
        "name": PKG,
        "files": [
            {
                "filename": f"{PKG_FILE}-1.0.0-py3-none-any.whl",
                "url": f"{base}/files/{PKG_FILE}-1.0.0-py3-none-any.whl",
                "hashes": {"sha256": sha_any},
                "requires-python": ">=3.8",
            }
        ],
    }


def corp_project_page(base: str) -> dict:
    page = project_page(base, sha_any="corp-sha-DIFFERENT")
    page["files"].append(
        {
            "filename": f"{PKG_FILE}-1.0.0+internal1-py3-none-any.whl",
            "url": f"{base}/files/{PKG_FILE}-1.0.0%2Binternal1-py3-none-any.whl",
            "hashes": {"sha256": "internal-only-sha"},
        }
    )
    return page


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, dict] = {}

    def do_GET(self):  # noqa: N802
        payload = self.routes.get(self.path.rstrip("/") + "/")
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", JSON_CT)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # тишина в тестовом выводе
        return


@pytest.fixture(scope="module")
def simple_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    _Handler.routes = {
        "/public/simple/": {"meta": {"api-version": "1.0"}, "projects": [{"name": PKG}]},
        f"/public/simple/{PKG}/": project_page(base, sha_any="public-sha-AAA"),
        "/corp/simple/": {"meta": {"api-version": "1.0"}, "projects": [{"name": PKG}]},
        f"/corp/simple/{PKG}/": corp_project_page(base),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield base
    server.shutdown()


def _session_factory():
    engine = create_async_engine(get_settings().async_db_url, poolclass=NullPool)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _upsert_registry(db, *, slug, registry_type, base_url, enumerate_enabled):
    from app.models.enums import Ecosystem, RegistryType

    row = await db.scalar(select(Registry).where(Registry.slug == slug))
    if row is None:
        row = Registry(
            ecosystem=Ecosystem.pypi,
            name=slug,
            slug=slug,
            registry_type=RegistryType(registry_type),
            protocol="pypi-simple",
            base_url=base_url,
            sync_priority=50,
            enumerate_enabled=enumerate_enabled,
        )
        db.add(row)
    else:
        row.base_url = base_url
        row.enumerate_enabled = enumerate_enabled
    await db.commit()
    await db.refresh(row)
    return row


def test_multiregistry_union_and_collisions(simple_server):
    async def scenario():
        engine, session_factory = _session_factory()
        try:
            async with session_factory() as db:
                corp = await _upsert_registry(
                    db,
                    slug="it-corp-hosted",
                    registry_type="corporate",
                    base_url=f"{simple_server}/corp/simple/",
                    enumerate_enabled=True,
                )
                public = await _upsert_registry(
                    db,
                    slug="it-public-sim",
                    registry_type="public",
                    base_url=f"{simple_server}/public/simple/",
                    enumerate_enabled=False,
                )

                # 1. Enumeration внутреннего hosted: пакет появляется в каталоге
                result = await _enumerate_registry(db, corp, batch_limit=50)
                assert result.get("skipped") is not True

                package = await db.scalar(select(Package).where(Package.normalized_name == PKG))
                assert package is not None, "enumeration должен создать пакет из листинга"

                # 2. То же имя из публичного реестра → union версий + коллизии
                connector = build_connector(registry_info_from_row(public))
                metadata = await connector.fetch_package_metadata(PKG)
                await run_sync_pipeline(metadata, db, registry=public)

                versions = (
                    await db.scalars(select(PackageVersion).where(PackageVersion.package_id == package.id))
                ).all()
                assert {v.version for v in versions} == {"1.0.0", "1.0.0+internal1"}

                artifacts = (
                    await db.scalars(
                        select(Artifact).where(Artifact.version_id.in_([v.id for v in versions]))
                    )
                ).all()
                registries_seen = {a.registry_id for a in artifacts}
                assert corp.id in registries_seen and public.id in registries_seen, (
                    "артефакты обеих реестров должны сосуществовать"
                )

                links = (
                    await db.scalars(select(PackageRegistry).where(PackageRegistry.package_id == package.id))
                ).all()
                assert len(links) == 2
                assert all(link.name_collision for link in links), "internal+public имя → флаг коллизии"
                assert all(link.checksum_mismatch for link in links), (
                    "одинаковый filename с разными sha256 → флаг расхождения"
                )
                primary_links = [link for link in links if link.is_primary]
                assert len(primary_links) == 1 and primary_links[0].registry_id == corp.id, (
                    "первый реестр (corp) остаётся primary при равном протоколе"
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_package_searchable_via_api(simple_server, client, viewer_token):
    response = client.get(
        "/api/v1/search",
        params={"q": PKG},
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert response.status_code == 200
    assert PKG in response.text
