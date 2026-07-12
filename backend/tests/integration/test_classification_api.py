"""Классификация в API (spec classification): карточка с вердиктом,
эффективные свойства, RBAC аннотаций, фасетные фильтры.

Детерминированно: probe-строка вставляется напрямую (без сети), классификация
вызывается синхронно; используется seed-пакет flask (кураторский override).
"""

import asyncio

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.classifier.service import classify_one_version, seed_rule_registries
from app.config import get_settings
from app.models.classification import UserCapability
from app.models.dist_probe import DistProbe
from app.models.package import Package
from app.models.version import PackageVersion

FLASK_ENTRY_POINTS = "[console_scripts]\nflask = flask.cli:main\n"


def _engine():
    return create_async_engine(get_settings().async_db_url, poolclass=NullPool)


async def _ensure_flask_classified() -> None:
    engine = _engine()
    try:
        async with async_sessionmaker(bind=engine, expire_on_commit=False)() as db:
            package = await db.scalar(select(Package).where(Package.normalized_name == "flask"))
            assert package is not None, "интеграционный контур должен быть засеян flask"
            version = await db.scalar(
                select(PackageVersion).where(
                    PackageVersion.package_id == package.id,
                    PackageVersion.version == package.latest_version,
                )
            )
            assert version is not None

            existing_probe = await db.scalar(
                select(DistProbe).where(
                    DistProbe.version_id == version.id, DistProbe.is_representative.is_(True)
                )
            )
            if existing_probe is None:
                db.add(
                    DistProbe(
                        version_id=version.id,
                        registry_slug="pypi",
                        artifact_filename=f"flask-{version.version}-py3-none-any.whl",
                        is_representative=True,
                        entry_points_txt=FLASK_ENTRY_POINTS,
                        top_level_txt="flask\n",
                        file_facts={"ext_counts": {"py": 30}, "platform_wheel": False, "scripts": []},
                    )
                )
                await db.flush()

            await seed_rule_registries(db)
            await classify_one_version(db, version, package)
            # чистим возможные пометки прошлых прогонов
            await db.execute(delete(UserCapability).where(UserCapability.name == "flask"))
            await db.commit()
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def classified_flask():
    asyncio.run(_ensure_flask_classified())


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_package_card_has_classification(client, viewer_token):
    response = client.get("/api/v1/packages/pypi/flask", headers=auth(viewer_token))
    assert response.status_code == 200
    body = response.json()
    block = body["classification"]
    assert block is not None
    # flask — кураторский override: терминальный набор ролей, статус ok
    roles = {f["role"] for f in block["facets"]}
    assert roles == {"runtime-component", "dev-tool", "dev-service"}
    assert block["status"] == "ok"
    assert any(f["evidence"] == "curated override" for f in block["facets"])
    caps = {c["capability"] for c in block["capabilities"]}
    assert "cli" in caps and "importable-api" in caps
    assert {e["name"] for e in block["effective_capabilities"]} >= {"cli", "importable-api"}
    assert isinstance(body["registries"], list) and body["registries"]


def test_role_filter_finds_flask(client, viewer_token):
    response = client.get(
        "/api/v1/packages", params={"role": "dev-service"}, headers=auth(viewer_token)
    )
    assert response.status_code == 200
    names = {item["normalized_name"] for item in response.json()["data"]}
    assert "flask" in names


def test_unknown_role_rejected(client, viewer_token):
    response = client.get("/api/v1/packages", params={"role": "nonsense"}, headers=auth(viewer_token))
    assert response.status_code == 422


def test_viewer_cannot_annotate(client, viewer_token):
    response = client.post(
        "/api/v1/annotate",
        json={"ecosystem": "pypi", "name": "flask", "capability": "tui-app", "present": True},
        headers=auth(viewer_token),
    )
    assert response.status_code == 403


def test_annotation_changes_effective_and_filters(client, admin_token, viewer_token):
    # куратор снимает importable-api у flask
    response = client.post(
        "/api/v1/annotate",
        json={
            "ecosystem": "pypi",
            "name": "flask",
            "capability": "importable-api",
            "present": False,
            "note": "интеграционный тест",
        },
        headers=auth(admin_token),
    )
    assert response.status_code == 200

    detail = client.get("/api/v1/packages/pypi/flask", headers=auth(viewer_token)).json()
    effective = {e["name"] for e in detail["classification"]["effective_capabilities"]}
    assert "importable-api" not in effective

    # role сужает выборку до классифицированных: у dev-service пакетов
    # importable-api эффективно отсутствует только у аннотированного flask
    filtered = client.get(
        "/api/v1/packages",
        params={"cap_none": "importable-api", "role": "dev-service", "ecosystem": "pypi"},
        headers=auth(viewer_token),
    ).json()
    assert "flask" in {item["normalized_name"] for item in filtered["data"]}

    # обратный фильтр: cap_all находит flask до снятия... после снятия — нет
    inverse = client.get(
        "/api/v1/packages",
        params={"cap_all": "importable-api", "role": "dev-service", "ecosystem": "pypi"},
        headers=auth(viewer_token),
    ).json()
    assert "flask" not in {item["normalized_name"] for item in inverse["data"]}

    # удаление пометки возвращает машинный факт
    response = client.post(
        "/api/v1/annotate/delete",
        json={"ecosystem": "pypi", "name": "flask", "capability": "importable-api", "version": ""},
        headers=auth(admin_token),
    )
    assert response.status_code == 200 and response.json()["deleted"] == 1

    detail = client.get("/api/v1/packages/pypi/flask", headers=auth(viewer_token)).json()
    effective = {e["name"] for e in detail["classification"]["effective_capabilities"]}
    assert "importable-api" in effective


def test_invalid_capability_rejected(client, admin_token):
    response = client.post(
        "/api/v1/annotate",
        json={"ecosystem": "pypi", "name": "flask", "capability": "not-a-cap", "present": True},
        headers=auth(admin_token),
    )
    assert response.status_code == 422


def test_llm_input_export_for_non_ok_verdicts(tmp_path):
    async def scenario():
        from app.classifier.export import export_llm_input

        engine = _engine()
        try:
            async with async_sessionmaker(bind=engine, expire_on_commit=False)() as db:
                package = await db.scalar(select(Package).where(Package.normalized_name == "requests"))
                assert package is not None, "requests должен быть засеян"
                version = await db.scalar(
                    select(PackageVersion).where(
                        PackageVersion.package_id == package.id,
                        PackageVersion.version == package.latest_version,
                    )
                )
                probe = await db.scalar(select(DistProbe).where(DistProbe.version_id == version.id))
                if probe is None:
                    db.add(
                        DistProbe(
                            version_id=version.id,
                            registry_slug="pypi",
                            artifact_filename="requests-x-py3-none-any.whl",
                            is_representative=True,
                            # неизвестная entry-point группа → needs-data (вход LLM-слоя)
                            entry_points_txt="[totally.unknown.group]\nx = y:z\n",
                            top_level_txt="requests\n",
                            file_facts={"ext_counts": {"py": 20}},
                        )
                    )
                    await db.flush()
                await seed_rule_registries(db)
                result = await classify_one_version(db, version, package)
                await db.commit()
                assert result.status == "needs-data"

                output = tmp_path / "llm_input.jsonl"
                exported = await export_llm_input(db, output, max_chars=500)
                assert exported >= 1
                import json as jsonlib

                records = [jsonlib.loads(line) for line in output.read_text().splitlines()]
                record = next(r for r in records if r["name"] == "requests")
                assert record["status"] == "needs-data"
                assert "totally.unknown.group" in record["status_reason"]
                assert isinstance(record["structural_roles"], list)
                assert "exported_at" in record and "descriptions" in record
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_stats_include_classification(client, viewer_token):
    body = client.get("/api/v1/stats", headers=auth(viewer_token)).json()
    block = body["classification"]
    assert block is not None
    assert block["total"] >= 1
    assert "runtime-component" in block["roles"] or "dev-tool" in block["roles"]
    assert block["effective_capabilities"].get("cli", 0) >= 1
