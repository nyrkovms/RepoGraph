import asyncio
from pathlib import Path

import pytest

from app.ecosystems.base import RegistryConfig
from app.schemas.registries import ProfileMember, ProfileUpsert, RegistryCreate, RegistryUpdate
from app.services import registries as service
from app.services.registries import (
    ProfileFields,
    RegistryFields,
    RegistryInvalid,
    changed_fields,
    merge_update,
    parse_import,
    plan_profile,
    plan_registry,
    validate_registry,
)


def _fields(**overrides) -> RegistryFields:
    values = {
        "slug": "mirror", "name": "Mirror", "ecosystem": "pypi", "protocol": "pypi-warehouse",
        "base_url": "https://mirror.invalid/", "priority": 100, "config": {}, "routing": None,
        "auth_type": "none", "credentials_ref": None,
    }
    return RegistryFields(**{**values, **overrides})


def test_valid_registry_passes() -> None:
    validate_registry(_fields())
    validate_registry(_fields(routing={"mode": "exclusive", "packages": ["torch"]}))
    validate_registry(_fields(auth_type="bearer", credentials_ref="MIRROR_TOKEN"))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ecosystem": "npm"}, "no plugin is registered for ecosystem 'npm'"),
        ({"ecosystem": "nope"}, "no plugin is registered"),
        ({"protocol": "gopher"}, "unknown protocol 'gopher'"),
        ({"protocol": "npm-registry"}, "no adapter for protocol 'npm-registry'"),
        ({"routing": {"mode": "sometimes", "packages": ["a"]}}, "unknown routing mode"),
        ({"routing": {"mode": "exclusive"}}, "needs routing.packages"),
        ({"routing": {"mode": "allow", "packages": "torch"}}, "must be a list of strings"),
        ({"auth_type": "digest"}, "auth_type must be one of"),
        ({"auth_type": "basic"}, "credentials_ref is required"),
        ({"auth_type": "tls_client_cert", "credentials_ref": ""}, "credentials_ref is required"),
        ({"base_url": "ftp://mirror.invalid/"}, "base_url must be an http"),
        ({"base_url": "https://user:secret@mirror.invalid/"}, "must not carry credentials"),
        ({"routing": {"pakages": ["torch"]}}, "unknown routing keys"),
        ({"config": {"negative_cache_ttl_seconds": "1h"}}, "must be a non-negative integer"),
        ({"config": {"negative_cache_ttl_seconds": -1}}, "must be a non-negative integer"),
        ({"config": {"api_url": "https://u:p@mirror.invalid/json"}}, "config.api_url must be an http"),
    ],
)
def test_invalid_registry_is_rejected_with_a_readable_message(overrides: dict, message: str) -> None:
    with pytest.raises(RegistryInvalid, match=message):
        validate_registry(_fields(**overrides))


def test_create_body_has_no_enabled_field_and_defaults() -> None:
    body = RegistryCreate(slug="m", name="M", ecosystem="pypi", protocol="pypi-warehouse", base_url="https://m/",
                          enabled=True)  # type: ignore[call-arg]
    assert "enabled" not in body.model_dump()
    assert (body.priority, body.config, body.routing, body.auth_type) == (100, {}, None, "none")


def test_update_changes_only_sent_fields() -> None:
    merged = merge_update(_fields(routing={"packages": ["a"]}), RegistryUpdate(priority=5, name="Renamed"))
    assert (merged.priority, merged.name, merged.routing) == (5, "Renamed", {"packages": ["a"]})


def test_update_can_clear_routing_and_credentials_with_null() -> None:
    current = _fields(auth_type="bearer", credentials_ref="T", routing={"packages": ["a"]})
    merged = merge_update(current, RegistryUpdate.model_validate({"routing": None, "credentials_ref": None}))
    assert (merged.routing, merged.credentials_ref) == (None, None)


def test_update_rejects_changes_of_immutable_fields_but_not_repeats() -> None:
    current = _fields()
    assert merge_update(current, RegistryUpdate(slug="mirror", ecosystem="pypi", protocol="pypi-warehouse")) == current
    for field, value in (("slug", "other"), ("ecosystem", "npm"), ("protocol", "pypi-simple")):
        with pytest.raises(RegistryInvalid, match=f"{field} cannot be changed"):
            merge_update(current, RegistryUpdate.model_validate({field: value}))


def test_update_rejects_null_for_required_fields() -> None:
    with pytest.raises(RegistryInvalid, match="priority cannot be null"):
        merge_update(_fields(), RegistryUpdate.model_validate({"priority": None}))


def test_plan_registry() -> None:
    current = _fields()
    assert plan_registry(None, current) == "created"
    assert plan_registry(current, _fields()) == "unchanged"
    assert plan_registry(current, _fields(priority=1)) == "updated"
    assert changed_fields(current, _fields(priority=1, routing={"packages": ["a"]})) == ["priority", "routing"]


def test_plan_profile() -> None:
    current = ProfileFields("Default", True, (("pypi", None), ("cu121", {"mode": "priority"})))
    assert plan_profile(None, current) == "created"
    assert plan_profile(current, ProfileFields.of(ProfileUpsert(
        name="Default", is_default=True,
        registries=[ProfileMember(slug="pypi"), ProfileMember(slug="cu121", routing_override={"mode": "priority"})],
    ))) == "unchanged"
    assert plan_profile(current, ProfileFields("Default", True, (("cu121", {"mode": "priority"}), ("pypi", None)))) == "updated"
    assert plan_profile(current, ProfileFields("Default", False, current.members)) == "updated"


def test_parse_import_mapping_and_bare_list() -> None:
    entry = {"slug": "m", "name": "M", "ecosystem": "pypi", "protocol": "pypi-warehouse", "base_url": "https://m/"}
    import yaml

    for raw in ({"registries": [entry], "profiles": []}, [entry]):
        document = parse_import(yaml.safe_dump(raw))
        assert [r.slug for r in document.registries] == ["m"]
        assert document.registries[0].enabled is None


def test_parse_import_rejects_bad_files() -> None:
    with pytest.raises(RegistryInvalid, match="not valid YAML"):
        parse_import("registries: [unclosed")
    with pytest.raises(RegistryInvalid, match="unknown top-level keys: sources"):
        parse_import("sources: []")
    with pytest.raises(RegistryInvalid, match="registries.0.slug"):
        parse_import("registries:\n  - name: no slug\n    ecosystem: pypi\n    protocol: pypi-warehouse\n    base_url: https://m/\n")
    with pytest.raises(RegistryInvalid, match="mapping"):
        parse_import("just text")


def test_example_file_is_importable_and_valid() -> None:
    path = Path(__file__).resolve().parents[2] / "registries.example.yaml"
    document = parse_import(path.read_text(encoding="utf-8"))
    assert [(r.slug, r.protocol) for r in document.registries] == [("pypi-mirror", "pypi-warehouse")]
    for registry in document.registries:
        validate_registry(RegistryFields.from_body(registry))
    assert [(p.ecosystem, p.slug, p.is_default, [m.slug for m in p.registries]) for p in document.profiles] == [
        ("pypi", "default", True, ["pypi-mirror"])
    ]


def test_example_pytorch_index_is_accepted() -> None:
    cuda = _fields(slug="pytorch-cu121", protocol="pypi-simple", base_url="https://download.pytorch.org/whl/cu121/",
                   routing={"mode": "exclusive", "packages": ["torch", "torchvision", "torchaudio"]})
    validate_registry(cuda)


# --- connection test ---------------------------------------------------------------------------------


class _Plugin:
    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour

    def adapter(self, registry: RegistryConfig):
        return self

    async def test_connection(self) -> None:
        await self.behaviour()


def _check(monkeypatch, behaviour, timeout: float = 1.0):
    monkeypatch.setattr(service, "plugin_for", lambda ecosystem: _Plugin(behaviour))
    return asyncio.run(service.check_connection(_fields().contract(), timeout))


def test_connection_ok(monkeypatch) -> None:
    async def fine() -> None:
        return None

    result = _check(monkeypatch, fine)
    assert (result.ok, result.error) == (True, None)
    assert result.elapsed_ms >= 0


def test_connection_exception_is_reported_with_its_type(monkeypatch) -> None:
    async def broken() -> None:
        raise ConnectionError("refused")

    result = _check(monkeypatch, broken)
    assert (result.ok, result.error) == (False, "ConnectionError: refused")


def test_timeout_raised_by_the_adapter_is_not_mistaken_for_the_deadline(monkeypatch) -> None:
    async def slow() -> None:
        raise TimeoutError("read timed out")

    result = _check(monkeypatch, slow)
    assert result.error == "TimeoutError: read timed out"


def test_connection_timeout(monkeypatch) -> None:
    async def hangs() -> None:
        await asyncio.sleep(10)

    result = _check(monkeypatch, hangs, timeout=0.05)
    assert result.ok is False
    assert result.error is not None and result.error.startswith("TimeoutError")


def test_pytorch_file_is_importable_and_valid() -> None:
    path = Path(__file__).resolve().parents[2] / "registries.pytorch.yaml"
    document = parse_import(path.read_text(encoding="utf-8"))
    assert [(r.slug, r.protocol, r.routing["mode"]) for r in document.registries] == [
        ("pytorch-cpu", "pypi-simple", "allow"), ("pytorch-cu121", "pypi-simple", "allow"),
        ("pytorch-cu128", "pypi-simple", "allow")]
    for registry in document.registries:
        validate_registry(RegistryFields.from_body(registry))
    assert {p.slug: [m.slug for m in p.registries] for p in document.profiles} == {
        "cpu": ["pytorch-cpu", "pypi"], "cuda121": ["pytorch-cu121", "pypi"], "cuda128": ["pytorch-cu128", "pypi"]}
    assert not any(p.is_default for p in document.profiles)
    # exclusive inside the profile only: the catalog keeps reading torch from pypi.org as well
    assert {p.slug: p.registries[0].routing_override["mode"] for p in document.profiles} == {
        "cpu": "exclusive", "cuda121": "exclusive", "cuda128": "exclusive"}
