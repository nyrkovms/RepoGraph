"""Сервис фактов по требованию (spec classification / on-demand-classification).

Использует реальный PyPI (как seed в этом контуре): холодный вызов для пакета
вне каталога обязан собрать факты за один запрос.
"""

COLD_PACKAGE = "sqlparse"  # маленький, стабильный, есть none-any wheel

FORBIDDEN_VERDICT_FIELDS = {"allow", "deny", "decision", "score", "verdict"}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_cold_call_assembles_facts(client, viewer_token):
    response = client.get(f"/api/v1/facts/pypi/{COLD_PACKAGE}", headers=auth(viewer_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["normalized_name"] == COLD_PACKAGE
    assert body["latest_version"]
    block = body["classification"]
    assert block is not None, "холодный вызов обязан собрать классификацию"
    assert block["version"] == body["latest_version"]
    assert block["classifier_version"]
    assert block["facets"], "роли должны быть назначены"
    assert body["registries"] and body["registries"][0]["slug"] == "pypi"


def test_repeat_call_served_from_catalog(client, viewer_token):
    first = client.get(f"/api/v1/facts/pypi/{COLD_PACKAGE}", headers=auth(viewer_token))
    assert first.status_code == 200
    second = client.get(f"/api/v1/facts/pypi/{COLD_PACKAGE}", headers=auth(viewer_token))
    assert second.status_code == 200
    # тот же вердикт (classified_at не пересчитывается повторно)
    assert second.json()["classification"]["classified_at"] == first.json()["classification"]["classified_at"]


def test_no_verdict_fields_in_contract(client, viewer_token):
    body = client.get(f"/api/v1/facts/pypi/{COLD_PACKAGE}", headers=auth(viewer_token)).json()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in FORBIDDEN_VERDICT_FIELDS, f"поле вердикта в ответе: {key}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)


def test_unknown_package_404(client, viewer_token):
    response = client.get(
        "/api/v1/facts/pypi/phub-definitely-not-a-real-package-xyz", headers=auth(viewer_token)
    )
    assert response.status_code == 404


def test_unknown_ecosystem_422(client, viewer_token):
    response = client.get("/api/v1/facts/maven/junit", headers=auth(viewer_token))
    assert response.status_code == 422


def test_requires_token(client):
    response = client.get(f"/api/v1/facts/pypi/{COLD_PACKAGE}")
    assert response.status_code == 401


def test_refresh_resyncs_metadata(client, viewer_token):
    body = client.get(f"/api/v1/facts/pypi/{COLD_PACKAGE}?refresh=true", headers=auth(viewer_token)).json()
    assert body["classification"] is not None
    assert body["version_status"] in ("active", "yanked", "deprecated", "unlisted")


# --- version-scoped facts (version-scoped-facts) ---------------------------

VERSIONED_PACKAGE = "six"
OLD_VERSION = "1.16.0"  # не latest (latest 1.17.0), кроссплатформенный wheel, стабилен


def test_version_scoped_facts_for_non_latest(client, viewer_token):
    response = client.get(
        f"/api/v1/facts/pypi/{VERSIONED_PACKAGE}?version={OLD_VERSION}", headers=auth(viewer_token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested_version"] == OLD_VERSION
    block = body["classification"]
    assert block is not None
    # факты относятся ИМЕННО к запрошенной версии, не к latest
    assert block["version"] == OLD_VERSION
    assert block["facets"]
    # six — библиотека
    assert "runtime-component" in {f["role"] for f in block["facets"]}


def test_version_scoped_does_not_move_latest(client, viewer_token):
    # запрос старой версии
    client.get(f"/api/v1/facts/pypi/{VERSIONED_PACKAGE}?version={OLD_VERSION}", headers=auth(viewer_token))
    # latest-факты по-прежнему про latest
    latest = client.get(f"/api/v1/facts/pypi/{VERSIONED_PACKAGE}", headers=auth(viewer_token)).json()
    assert latest["requested_version"] == latest["latest_version"]
    assert latest["latest_version"] != OLD_VERSION
    assert latest["classification"]["version"] == latest["latest_version"]


def test_nonexistent_version_404(client, viewer_token):
    response = client.get(
        f"/api/v1/facts/pypi/{VERSIONED_PACKAGE}?version=99.99.99", headers=auth(viewer_token)
    )
    assert response.status_code == 404


def test_version_scoped_no_verdict_fields(client, viewer_token):
    body = client.get(
        f"/api/v1/facts/pypi/{VERSIONED_PACKAGE}?version={OLD_VERSION}", headers=auth(viewer_token)
    ).json()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in FORBIDDEN_VERDICT_FIELDS, f"поле вердикта: {key}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
