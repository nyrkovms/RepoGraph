from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.resolver.errors import (
    ResolveConflictError,
    ResolveInternalError,
    ResolveNotFoundError,
    ResolveUpstreamTimeoutError,
)
from app.resolver.service import resolve_dependencies
from app.schemas.resolve import ErrorResponse, ResolveRequest, ResolveSuccessResponse

router = APIRouter()


@router.post(
    "/resolve",
    response_model=ResolveSuccessResponse,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def resolve_endpoint(payload: ResolveRequest):
    try:
        return await resolve_dependencies(payload)
    except ResolveConflictError as exc:
        return _error(409, "resolver_conflict", str(exc), details=exc.details)
    except ResolveNotFoundError as exc:
        return _error(404, "not_found", str(exc), details={"package": exc.package})
    except ResolveUpstreamTimeoutError as exc:
        return _error(504, "upstream_timeout", str(exc), details={"package": exc.package})
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
