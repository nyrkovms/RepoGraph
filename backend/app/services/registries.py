"""Sources (registries) and source profiles: validation and storage shared by the API and the CLI
(SPEC-MULTI-ECOSYSTEM.md §6.2, §6.4, §11.2).

The functions flush but do not commit (the caller owns the transaction), except `import_document`, which commits
per object so one bad entry does not undo the others.
"""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ecosystems.base import EcosystemPlugin, RegistryConfig, SourceProfile
from app.ecosystems.registry import UnknownEcosystem
from app.fetcher.pipeline import plugin_for
from app.fetcher.sources import registry_config
from app.models.enums import Ecosystem, RegistryProtocol
from app.models.package_registry import PackageRegistry
from app.models.registry import Registry
from app.models.source_profile import SourceProfile as SourceProfileRow
from app.models.source_profile import SourceProfileRegistry
from app.models.sync_state import SyncState
from app.schemas.registries import (
    ConnectionTestResponse,
    ImportDocument,
    ProfileImport,
    ProfileMember,
    ProfileResponse,
    ProfileUpsert,
    RegistryCreate,
    RegistryImport,
    RegistryResponse,
    RegistryState,
    RegistryUpdate,
)

logger = logging.getLogger(__name__)

AUTH_TYPES = ("none", "basic", "bearer", "tls_client_cert")
TEST_TIMEOUT_SECONDS = 30.0
IMMUTABLE_FIELDS = ("slug", "ecosystem", "protocol")


class RegistryError(Exception):
    """Base of the errors the API maps to HTTP statuses."""


class RegistryNotFound(RegistryError):
    pass


class RegistryInvalid(RegistryError):
    pass


class RegistryConflict(RegistryError):
    pass


class ConnectionFailed(RegistryError):
    pass


# --- registry fields and validation -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryFields:
    slug: str
    name: str
    ecosystem: str
    protocol: str
    base_url: str
    priority: int
    config: dict
    routing: dict | None
    auth_type: str
    credentials_ref: str | None

    @classmethod
    def from_body(cls, body: RegistryCreate) -> "RegistryFields":
        return cls(**body.model_dump(include=set(cls.__dataclass_fields__)))

    @classmethod
    def from_row(cls, row: Registry) -> "RegistryFields":
        return cls(
            slug=row.slug, name=row.name, ecosystem=row.ecosystem.value,
            protocol=row.protocol.value if row.protocol else "", base_url=row.base_url, priority=row.priority,
            config=row.config or {}, routing=row.routing, auth_type=row.auth_type, credentials_ref=row.credentials_ref,
        )

    def contract(self) -> RegistryConfig:
        return RegistryConfig(
            id="validate", slug=self.slug, ecosystem=self.ecosystem, protocol=self.protocol,
            base_url=self.base_url, priority=self.priority, config=self.config, routing=self.routing,
            auth_type=self.auth_type, credentials_ref=self.credentials_ref,
        )


# Names that parse in every plugin's notation (npm scope, Maven coordinates, Go and RPM paths are tried in turn).
_PROBE_NAMES = ("x", "x:x", "@x/x", "example.com/x", "x/x")


def _plugin(ecosystem: str) -> EcosystemPlugin:
    try:
        return plugin_for(ecosystem)
    except UnknownEcosystem:
        raise RegistryInvalid(f"no plugin is registered for ecosystem {ecosystem!r}") from None


def _check_routing(plugin: EcosystemPlugin, config: RegistryConfig, what: str = "routing") -> None:
    """The plugin's routing parser raises ValueError on a bad rule; it only runs when a source is asked for a ref."""
    for name in _PROBE_NAMES:
        try:
            ref = plugin.coordinates.parse(name)
        except ValueError:
            continue
        try:
            plugin.sources_for(ref, SourceProfile(ecosystem=config.ecosystem, slug="validate", registries=[config]))
        except ValueError as exc:
            raise RegistryInvalid(f"invalid {what} for {config.slug}: {exc}") from None
        return
    raise RegistryInvalid(f"cannot validate {what}: the {config.ecosystem} plugin parses none of {_PROBE_NAMES}")


def validate_registry(fields: RegistryFields) -> None:
    plugin = _plugin(fields.ecosystem)
    if fields.protocol not in {p.value for p in RegistryProtocol}:
        raise RegistryInvalid(f"unknown protocol {fields.protocol!r}; known: {', '.join(p.value for p in RegistryProtocol)}")
    if fields.auth_type not in AUTH_TYPES:
        raise RegistryInvalid(f"auth_type must be one of {', '.join(AUTH_TYPES)}, got {fields.auth_type!r}")
    if fields.auth_type != "none" and not fields.credentials_ref:
        raise RegistryInvalid(f"credentials_ref is required for auth_type {fields.auth_type!r}")
    if not fields.base_url.startswith(("http://", "https://")):
        raise RegistryInvalid(f"base_url must be an http(s) URL, got {fields.base_url!r}")
    if urlsplit(fields.base_url).username is not None:
        # The URL is shown by GET /registries and lands in error messages; secrets go through credentials_ref.
        raise RegistryInvalid("base_url must not carry credentials; use auth_type and credentials_ref")
    ttl = fields.config.get("negative_cache_ttl_seconds")
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 0):
        raise RegistryInvalid("config.negative_cache_ttl_seconds must be a non-negative integer")
    for key in ("api_url", "simple_url"):
        url = fields.config.get(key)
        if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))
                                or urlsplit(url).username is not None):
            raise RegistryInvalid(f"config.{key} must be an http(s) URL without credentials")
    config = fields.contract()
    try:
        plugin.adapter(config)
    except ValueError as exc:
        raise RegistryInvalid(str(exc)) from None
    _check_routing(plugin, config)


def changed_fields(existing: RegistryFields, desired: RegistryFields) -> list[str]:
    return [name for name in RegistryFields.__dataclass_fields__ if getattr(existing, name) != getattr(desired, name)]


# --- reading ------------------------------------------------------------------------------------------


def _state_view(state: SyncState | None, now: datetime) -> RegistryState | None:
    if state is None:
        return None
    return RegistryState(
        status=state.status.value, last_success_at=state.last_success_at, last_error=state.last_error,
        consecutive_failures=state.consecutive_failures, degraded_until=state.degraded_until,
        degraded=state.degraded_until is not None and state.degraded_until > now, cursor_data=state.cursor_data or {},
    )


async def _views(db: AsyncSession, rows: list[Registry]) -> list[RegistryResponse]:
    ids = [row.id for row in rows]
    states = {
        str(s.registry_id): s for s in (await db.scalars(select(SyncState).where(SyncState.registry_id.in_(ids)))).all()
    }
    counts = dict(
        (str(registry_id), count)
        for registry_id, count in (
            await db.execute(
                select(PackageRegistry.registry_id, func.count())
                .where(PackageRegistry.registry_id.in_(ids), PackageRegistry.missing_since.is_(None))
                .group_by(PackageRegistry.registry_id)
            )
        ).all()
    )
    now = datetime.now(UTC)
    return [
        RegistryResponse(
            slug=row.slug, name=row.name, ecosystem=row.ecosystem.value,
            protocol=row.protocol.value if row.protocol else None, base_url=row.base_url, priority=row.priority,
            enabled=row.enabled, routing=row.routing, config=row.config or {}, auth_type=row.auth_type,
            credentials_ref=row.credentials_ref, state=_state_view(states.get(str(row.id)), now),
            packages=counts.get(str(row.id), 0),
        )
        for row in rows
    ]


async def _row(db: AsyncSession, slug: str) -> Registry:
    row = await db.scalar(select(Registry).where(Registry.slug == slug))
    if row is None:
        raise RegistryNotFound(f"registry {slug!r} not found")
    return row


async def list_registries(db: AsyncSession, ecosystem: str | None = None) -> list[RegistryResponse]:
    query = select(Registry).order_by(Registry.ecosystem, Registry.priority, Registry.slug)
    if ecosystem is not None:
        query = query.where(Registry.ecosystem == Ecosystem(ecosystem))
    return await _views(db, list((await db.scalars(query)).all()))


async def get_registry(db: AsyncSession, slug: str) -> RegistryResponse:
    return (await _views(db, [await _row(db, slug)]))[0]


# --- writing ------------------------------------------------------------------------------------------


def _apply(row: Registry, fields: RegistryFields) -> None:
    row.name = fields.name
    row.base_url = fields.base_url
    row.priority = fields.priority
    row.config = fields.config
    row.routing = fields.routing
    row.auth_type = fields.auth_type
    row.credentials_ref = fields.credentials_ref


async def create_registry(db: AsyncSession, body: RegistryCreate) -> RegistryResponse:
    fields = RegistryFields.from_body(body)
    validate_registry(fields)
    if await db.scalar(select(Registry.id).where(Registry.slug == fields.slug)) is not None:
        raise RegistryConflict(f"registry {fields.slug!r} already exists")
    row = Registry(
        ecosystem=Ecosystem(fields.ecosystem), slug=fields.slug, protocol=RegistryProtocol(fields.protocol),
        enabled=False,  # §6.4: a source is enabled only after a successful test
    )
    _apply(row, fields)
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:  # lost a race with another request creating the same slug
        raise RegistryConflict(f"registry {fields.slug!r} already exists") from None
    return (await _views(db, [row]))[0]


def merge_update(current: RegistryFields, body: RegistryUpdate) -> RegistryFields:
    sent = body.model_fields_set
    for name in IMMUTABLE_FIELDS:
        value = getattr(body, name)
        if name in sent and value != getattr(current, name):
            raise RegistryInvalid(f"{name} cannot be changed (registry {current.slug})")
    changes: dict[str, Any] = {}
    for name in ("name", "base_url", "priority", "config", "auth_type"):
        if name in sent:
            if getattr(body, name) is None:
                raise RegistryInvalid(f"{name} cannot be null")
            changes[name] = getattr(body, name)
    for name in ("routing", "credentials_ref"):  # null clears them
        if name in sent:
            changes[name] = getattr(body, name)
    return replace(current, **changes)


async def update_registry(db: AsyncSession, slug: str, body: RegistryUpdate) -> RegistryResponse:
    row = await _row(db, slug)
    fields = merge_update(RegistryFields.from_row(row), body)
    validate_registry(fields)
    _apply(row, fields)
    await db.flush()
    return (await _views(db, [row]))[0]


async def delete_registry(db: AsyncSession, slug: str) -> None:
    row = await _row(db, slug)
    in_use = await db.scalar(select(PackageRegistry.package_id).where(PackageRegistry.registry_id == row.id).limit(1))
    if in_use is not None:
        raise RegistryConflict(f"registry {slug!r} holds packages; disable it instead of deleting")
    await db.execute(delete(SourceProfileRegistry).where(SourceProfileRegistry.registry_id == row.id))
    await db.execute(delete(SyncState).where(SyncState.registry_id == row.id))
    await db.delete(row)
    try:
        await db.flush()
    except IntegrityError:  # other catalog rows (artifacts, failures, ...) still point at it
        await db.rollback()
        raise RegistryConflict(f"registry {slug!r} is referenced by catalog data; disable it instead") from None


async def set_enabled(db: AsyncSession, slug: str, enabled: bool) -> RegistryResponse:
    row = await _row(db, slug)
    # Only the flag: a disabled source is no longer asked, and the pipeline keeps its data frozen (SPEC §6.4).
    await db.execute(update(Registry).where(Registry.id == row.id).values(enabled=enabled))
    await db.refresh(row)
    return (await _views(db, [row]))[0]


# --- connection test ----------------------------------------------------------------------------------


async def check_connection(config: RegistryConfig, timeout: float = TEST_TIMEOUT_SECONDS) -> ConnectionTestResponse:
    started = perf_counter()
    error: str | None = None
    deadline = asyncio.timeout(timeout)
    try:
        adapter = plugin_for(config.ecosystem).adapter(config)
        async with deadline:
            await adapter.test_connection()
    except Exception as exc:  # noqa: BLE001 - any failure is the answer of the test
        # An adapter may raise TimeoutError itself; only an expired deadline means the source did not answer.
        error = f"TimeoutError: no answer within {timeout:g} s" if deadline.expired() else f"{type(exc).__name__}: {exc}"
    return ConnectionTestResponse(ok=error is None, error=error, elapsed_ms=round((perf_counter() - started) * 1000, 1))


async def run_connection_test(db: AsyncSession, slug: str) -> ConnectionTestResponse:
    config = registry_config(await _row(db, slug))
    await db.rollback()  # do not hold a transaction open while the source answers
    return await check_connection(config)


async def enable_registry(db: AsyncSession, slug: str) -> RegistryResponse:
    result = await run_connection_test(db, slug)
    if not result.ok:
        raise ConnectionFailed(f"registry {slug!r} stays disabled: {result.error}")
    return await set_enabled(db, slug, True)


# --- profiles -----------------------------------------------------------------------------------------


async def _profile_views(db: AsyncSession, profiles: list[SourceProfileRow]) -> list[ProfileResponse]:
    members: dict[Any, list[ProfileMember]] = {profile.id: [] for profile in profiles}
    rows = (
        await db.execute(
            select(SourceProfileRegistry.profile_id, Registry.slug, SourceProfileRegistry.routing_override)
            .join(Registry, Registry.id == SourceProfileRegistry.registry_id)
            .where(SourceProfileRegistry.profile_id.in_(list(members)))
            .order_by(SourceProfileRegistry.profile_id, SourceProfileRegistry.position)
        )
    ).all()
    for profile_id, slug, override in rows:
        members[profile_id].append(ProfileMember(slug=slug, routing_override=override))
    return [
        ProfileResponse(
            slug=p.slug, name=p.name, ecosystem=p.ecosystem.value, is_default=p.is_default, registries=members[p.id]
        )
        for p in profiles
    ]


async def list_profiles(db: AsyncSession, ecosystem: str | None = None) -> list[ProfileResponse]:
    query = select(SourceProfileRow).order_by(SourceProfileRow.ecosystem, SourceProfileRow.slug)
    if ecosystem is not None:
        query = query.where(SourceProfileRow.ecosystem == Ecosystem(ecosystem))
    return await _profile_views(db, list((await db.scalars(query)).all()))


async def _load_profile(db: AsyncSession, ecosystem: str, slug: str) -> SourceProfileRow | None:
    return await db.scalar(
        select(SourceProfileRow).where(SourceProfileRow.ecosystem == Ecosystem(ecosystem), SourceProfileRow.slug == slug)
    )


async def _profile_members(db: AsyncSession, body: ProfileUpsert, ecosystem: str) -> list[tuple[Registry, dict | None]]:
    plugin = _plugin(ecosystem)
    slugs = [member.slug for member in body.registries]
    duplicates = sorted({s for s in slugs if slugs.count(s) > 1})
    if duplicates:
        raise RegistryInvalid(f"registries listed twice: {', '.join(duplicates)}")
    rows = {r.slug: r for r in (await db.scalars(select(Registry).where(Registry.slug.in_(slugs)))).all()}
    members = []
    for member in body.registries:
        row = rows.get(member.slug)
        if row is None:
            raise RegistryInvalid(f"registry {member.slug!r} does not exist")
        if row.ecosystem.value != ecosystem:
            raise RegistryInvalid(f"registry {member.slug!r} belongs to {row.ecosystem.value}, not {ecosystem}")
        if member.routing_override is not None:
            _check_routing(plugin, replace(RegistryFields.from_row(row), routing=member.routing_override).contract(),
                           "routing_override")
        members.append((row, member.routing_override))
    return members


async def upsert_profile(db: AsyncSession, ecosystem: str, slug: str, body: ProfileUpsert) -> ProfileResponse:
    if ecosystem not in {e.value for e in Ecosystem}:
        raise RegistryInvalid(f"unknown ecosystem {ecosystem!r}")
    members = await _profile_members(db, body, ecosystem)

    profile = await _load_profile(db, ecosystem, slug)
    if profile is None:
        profile = SourceProfileRow(ecosystem=Ecosystem(ecosystem), slug=slug, name=body.name, is_default=False)
        db.add(profile)
    profile.name = body.name
    if body.is_default:
        # One default per ecosystem (partial unique index): clear the others before this one is set.
        await db.execute(
            update(SourceProfileRow)
            .where(SourceProfileRow.ecosystem == Ecosystem(ecosystem), SourceProfileRow.slug != slug)
            .values(is_default=False)
        )
    profile.is_default = body.is_default
    await db.flush()

    await db.execute(delete(SourceProfileRegistry).where(SourceProfileRegistry.profile_id == profile.id))
    for position, (row, override) in enumerate(members):
        db.add(SourceProfileRegistry(
            profile_id=profile.id, registry_id=row.id, ecosystem=profile.ecosystem, position=position,
            routing_override=override,
        ))
    await db.flush()
    return (await _profile_views(db, [profile]))[0]


# --- import file --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileFields:
    name: str
    is_default: bool
    members: tuple[tuple[str, dict | None], ...]

    @classmethod
    def of(cls, body: ProfileUpsert | ProfileResponse) -> "ProfileFields":
        return cls(body.name, body.is_default, tuple((m.slug, m.routing_override) for m in body.registries))


def plan_registry(existing: RegistryFields | None, desired: RegistryFields) -> str:
    if existing is None:
        return "created"
    return "updated" if changed_fields(existing, desired) else "unchanged"


def plan_profile(existing: ProfileFields | None, desired: ProfileFields) -> str:
    if existing is None:
        return "created"
    return "unchanged" if existing == desired else "updated"


def parse_import(text: str) -> ImportDocument:
    """A mapping with `registries` and `profiles`, or a bare list of registries."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryInvalid(f"not valid YAML: {exc}") from None
    if raw is None:
        raw = {}
    if isinstance(raw, list):
        raw = {"registries": raw}
    if not isinstance(raw, dict):
        raise RegistryInvalid("the file must be a mapping with `registries` and `profiles`, or a list of registries")
    unknown = sorted(set(raw) - {"registries", "profiles"})
    if unknown:
        raise RegistryInvalid(f"unknown top-level keys: {', '.join(map(str, unknown))}")
    try:
        return ImportDocument.model_validate(raw)
    except ValidationError as exc:
        raise RegistryInvalid(describe_validation_error(exc)) from None


def describe_validation_error(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())


@dataclass(slots=True)
class ImportResult:
    kind: str                 # registry | profile
    name: str
    outcome: str              # created | updated | unchanged | error
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome == "error"

    def line(self) -> str:
        return f"{self.outcome:<10}{self.kind} {self.name}" + (f": {self.detail}" if self.detail else "")


async def _import_registry(db: AsyncSession, item: RegistryImport) -> ImportResult:
    fields = RegistryFields.from_body(item)
    row = await db.scalar(select(Registry).where(Registry.slug == fields.slug))
    existing = RegistryFields.from_row(row) if row else None
    was_enabled = row.enabled if row else False
    outcome = plan_registry(existing, fields)
    if existing is not None and (existing.ecosystem, existing.protocol) != (fields.ecosystem, fields.protocol):
        raise RegistryInvalid("ecosystem and protocol of an existing registry cannot be changed")

    validate_registry(fields)
    if row is None:
        await create_registry(db, item)
    elif outcome == "updated":
        _apply(row, fields)
    await db.commit()

    # `enabled` in the file is a request: switching on needs a passing test, like POST .../enable.
    if item.enabled is False and was_enabled:
        await set_enabled(db, fields.slug, False)
        await db.commit()
        return ImportResult("registry", fields.slug, "updated" if outcome == "unchanged" else outcome, "disabled")
    if item.enabled and not was_enabled:
        try:
            await enable_registry(db, fields.slug)
        except ConnectionFailed as exc:
            return ImportResult("registry", fields.slug, "error", f"{outcome}, but left disabled: {exc}")
        await db.commit()
        return ImportResult("registry", fields.slug, "updated" if outcome == "unchanged" else outcome, "enabled")
    return ImportResult("registry", fields.slug, outcome)


async def _import_profile(db: AsyncSession, item: ProfileImport) -> ImportResult:
    existing = None
    profile = await _load_profile(db, item.ecosystem, item.slug) if item.ecosystem in {e.value for e in Ecosystem} else None
    if profile is not None:
        existing = ProfileFields.of((await _profile_views(db, [profile]))[0])
    outcome = plan_profile(existing, ProfileFields.of(item))
    await upsert_profile(db, item.ecosystem, item.slug, item)
    await db.commit()
    return ImportResult("profile", f"{item.ecosystem}/{item.slug}", outcome)


async def import_document(db: AsyncSession, document: ImportDocument) -> list[ImportResult]:
    """Registries first, then profiles. Idempotent: a second run over the same file reports `unchanged`."""
    results = []
    for registry in document.registries:
        try:
            results.append(await _import_registry(db, registry))
        except RegistryError as exc:
            await db.rollback()
            results.append(ImportResult("registry", registry.slug, "error", str(exc)))
    for profile in document.profiles:
        try:
            results.append(await _import_profile(db, profile))
        except RegistryError as exc:
            await db.rollback()
            results.append(ImportResult("profile", f"{profile.ecosystem}/{profile.slug}", "error", str(exc)))
    return results
