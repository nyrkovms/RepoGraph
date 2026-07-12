from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.admin import router as admin_router
from app.api.v1.packages import router as packages_router
from app.api.v1.resolve import router as resolve_router
from app.api.v1.search import router as search_router
from app.api.v1.system import router as system_router
from app.config import get_settings
from app.logging_config import setup_logging
from app.services.metrics import observe_http_request

setup_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    errors = settings.production_baseline_errors()
    if errors:
        raise RuntimeError("Production baseline check failed: " + " | ".join(errors))
    yield


app = FastAPI(
    title="PackageHub MVP API",
    version="0.1.0",
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts_list)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = perf_counter()
    response = await call_next(request)
    elapsed = perf_counter() - start

    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    observe_http_request(
        method=request.method,
        route=route_path,
        status_code=response.status_code,
        elapsed_seconds=elapsed,
    )
    return response


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    if settings.security_hsts_enabled:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.include_router(system_router, prefix="/api/v1", tags=["system"])
app.include_router(packages_router, prefix="/api/v1", tags=["packages"])
app.include_router(search_router, prefix="/api/v1", tags=["search"])
app.include_router(resolve_router, prefix="/api/v1", tags=["resolve"])
app.include_router(admin_router, prefix="/api/v1/admin", tags=["admin"])
