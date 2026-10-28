"""Sources and source profiles (SPEC-MULTI-ECOSYSTEM.md §11.2). Reads are open like the other GETs; changes are admin."""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import require_admin
from app.db import get_db_session
from app.models.enums import Ecosystem
from app.schemas.registries import (
    ConnectionTestResponse,
    ProfileListResponse,
    ProfileResponse,
    ProfileUpsert,
    RegistryCreate,
    RegistryListResponse,
    RegistryResponse,
    RegistryUpdate,
)
from app.services import registries as service

router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(require_admin)])

_STATUS = {
    service.RegistryNotFound: status.HTTP_404_NOT_FOUND,
    service.RegistryInvalid: 422,
    service.RegistryConflict: status.HTTP_409_CONFLICT,
    service.ConnectionFailed: status.HTTP_409_CONFLICT,
}


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except service.RegistryError as exc:
        raise HTTPException(status_code=_STATUS[type(exc)], detail=str(exc)) from None


@router.get("/registries", response_model=RegistryListResponse)
async def list_registries(
    ecosystem: Ecosystem | None = Query(default=None), db: AsyncSession = Depends(get_db_session)
) -> RegistryListResponse:
    return RegistryListResponse(data=await service.list_registries(db, ecosystem.value if ecosystem else None))


@router.get("/registries/{slug}", response_model=RegistryResponse)
async def get_registry(slug: str, db: AsyncSession = Depends(get_db_session)) -> RegistryResponse:
    with _http_errors():
        return await service.get_registry(db, slug)


@router.get("/profiles", response_model=ProfileListResponse)
async def list_profiles(
    ecosystem: Ecosystem | None = Query(default=None), db: AsyncSession = Depends(get_db_session)
) -> ProfileListResponse:
    return ProfileListResponse(data=await service.list_profiles(db, ecosystem.value if ecosystem else None))


@admin_router.post("/registries", response_model=RegistryResponse, status_code=status.HTTP_201_CREATED)
async def create_registry(body: RegistryCreate, db: AsyncSession = Depends(get_db_session)) -> RegistryResponse:
    with _http_errors():
        created = await service.create_registry(db, body)
    await db.commit()
    return created


@admin_router.put("/registries/{slug}", response_model=RegistryResponse)
async def update_registry(slug: str, body: RegistryUpdate, db: AsyncSession = Depends(get_db_session)) -> RegistryResponse:
    with _http_errors():
        updated = await service.update_registry(db, slug, body)
    await db.commit()
    return updated


@admin_router.post("/registries/{slug}/test", response_model=ConnectionTestResponse)
async def test_registry(slug: str, db: AsyncSession = Depends(get_db_session)) -> ConnectionTestResponse:
    with _http_errors():
        return await service.run_connection_test(db, slug)


@admin_router.post("/registries/{slug}/enable", response_model=RegistryResponse)
async def enable_registry(slug: str, db: AsyncSession = Depends(get_db_session)) -> RegistryResponse:
    with _http_errors():
        enabled = await service.enable_registry(db, slug)
    await db.commit()
    return enabled


@admin_router.post("/registries/{slug}/disable", response_model=RegistryResponse)
async def disable_registry(slug: str, db: AsyncSession = Depends(get_db_session)) -> RegistryResponse:
    with _http_errors():
        disabled = await service.set_enabled(db, slug, False)
    await db.commit()
    return disabled


@admin_router.delete("/registries/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_registry(slug: str, db: AsyncSession = Depends(get_db_session)) -> Response:
    with _http_errors():
        await service.delete_registry(db, slug)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.put("/profiles/{ecosystem}/{slug}", response_model=ProfileResponse)
async def upsert_profile(
    ecosystem: str, slug: str, body: ProfileUpsert, db: AsyncSession = Depends(get_db_session)
) -> ProfileResponse:
    with _http_errors():
        profile = await service.upsert_profile(db, ecosystem, slug, body)
    await db.commit()
    return profile
