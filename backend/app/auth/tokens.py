from enum import Enum
from hmac import compare_digest

from fastapi import Depends, Header, HTTPException, status

from app.config import get_settings


class Role(str, Enum):
    viewer = "viewer"
    admin = "admin"


async def get_current_role(authorization: str | None = Header(default=None)) -> Role:
    settings = get_settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")

    token = authorization.removeprefix("Bearer ").strip()
    if compare_digest(token, settings.api_token_admin):
        return Role.admin
    if compare_digest(token, settings.api_token_viewer):
        return Role.viewer

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")


async def require_viewer(role: Role = Depends(get_current_role)) -> Role:
    if role not in (Role.viewer, Role.admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return role


async def require_admin(role: Role = Depends(get_current_role)) -> Role:
    if role is not Role.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return role
