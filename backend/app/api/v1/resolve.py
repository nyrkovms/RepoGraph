from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.resolver.errors import (
    ResolveConflictError,
    ResolveInternalError,
    ResolveInvalidRequest,
    ResolveLimitError,
    ResolveNotFoundError,
    ResolveTimeoutError,
    ResolveUpstreamError,
)
from app.resolver.service import resolve_dependencies, resolve_graph
from app.schemas.resolve import (
    ErrorResponse,
    GraphResolveRequest,
    GraphResolveResponse,
    ResolveRequest,
    ResolveSuccessResponse,
)

router = APIRouter()


@router.post(
    "/resolve",
    response_model=GraphResolveResponse | ResolveSuccessResponse,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def resolve_endpoint(
    payload: GraphResolveRequest | ResolveRequest = Body(...),
    db: AsyncSession = Depends(get_db_session),
):
    """`roots` asks for the graph (SPEC §9.1): failures of the whole graph come back in its `error` with 200.
    `dependencies` is the MVP nested format, an alias until E7, with failures as HTTP errors."""
    try:
        if isinstance(payload, GraphResolveRequest):
            return await resolve_graph(db, payload)
        return await resolve_dependencies(db, payload)
    except ResolveInvalidRequest as exc:
        return _error(422, "invalid_request", str(exc))
    except ResolveConflictError as exc:
        return _error(409, "resolver_conflict", str(exc), details=exc.details)
    except ResolveNotFoundError as exc:
        return _error(404, "not_found", str(exc), details={"package": exc.package})
    except ResolveLimitError as exc:
        return _error(422, "resolve_limit_exceeded", str(exc), details=exc.details)
    except ResolveTimeoutError as exc:
        return _error(504, "resolve_timeout", str(exc))
    except ResolveUpstreamError as exc:
        return _error(502, "upstream_error", str(exc))
    except ResolveInternalError as exc:
        return _error(500, "internal_error", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(500, "internal_error", f"Unhandled resolve error: {exc}")


def _error(status_code: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "trace_id": None,
            }
        },
    )
