from hmac import compare_digest

from fastapi import Header, HTTPException, status

from app.config import get_settings


# Read endpoints are open: access to them is limited at the network level.
# Only admin operations (/admin/*) require a token.
async def require_admin(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")

    token = authorization.removeprefix("Bearer ").strip()
    if not compare_digest(token, get_settings().api_token_admin):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")
