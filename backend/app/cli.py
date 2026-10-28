import argparse
import asyncio
import json
import sys

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.logging_config import setup_logging
from app.models.enums import Ecosystem
from app.services import registries as registries_service
from app.tasks.fetch_deps import _fetch_package_async
from app.tasks.refresh import _run_refresh_async
from app.tasks.retry_failed import _run_retry_failed_async
from app.tasks.seed import _run_seed_async
from app.models.dependency import Dependency
from app.models.package import Package
from app.models.version import PackageVersion


async def _stats() -> dict[str, int]:
    async with SessionLocal() as db:
        packages = await db.scalar(select(func.count(Package.id)).where(Package.ecosystem == "pypi"))
        versions = await db.scalar(select(func.count(PackageVersion.id)))
        dependencies = await db.scalar(select(func.count(Dependency.id)).where(Dependency.variant_registry_id.is_(None)))

    return {
        "packages": int(packages or 0),
        "versions": int(versions or 0),
        "dependencies": int(dependencies or 0),
    }


async def _registries_import(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        document = registries_service.parse_import(handle.read())
    async with SessionLocal() as db:
        results = await registries_service.import_document(db, document)
    for result in results:
        print(result.line())
    return 1 if any(result.failed for result in results) else 0


async def _registries_test(slug: str) -> int:
    async with SessionLocal() as db:
        result = await registries_service.run_connection_test(db, slug)
    print("ok" if result.ok else f"failed: {result.error}", f"({result.elapsed_ms:.0f} ms)")
    return 0 if result.ok else 1


async def _registries_list(ecosystem: str | None) -> int:
    async with SessionLocal() as db:
        rows = await registries_service.list_registries(db, ecosystem)
    table = [("SLUG", "ECOSYSTEM", "PROTOCOL", "PRIORITY", "ENABLED", "STATE", "PACKAGES")]
    for row in rows:
        state = "-"
        if row.state is not None:
            state = "degraded" if row.state.degraded else row.state.status
        table.append((row.slug, row.ecosystem, row.protocol or "-", str(row.priority), "yes" if row.enabled else "no",
                      state, str(row.packages)))
    widths = [max(len(line[i]) for line in table) for i in range(len(table[0]))]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip())
    return 0


def _run_registries(args: argparse.Namespace) -> int:
    try:
        if args.registries_command == "import":
            return asyncio.run(_registries_import(args.file))
        if args.registries_command == "test":
            return asyncio.run(_registries_test(args.slug))
        return asyncio.run(_registries_list(args.ecosystem))
    except (OSError, registries_service.RegistryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_cmd = sub.add_parser("seed")
    seed_cmd.add_argument("--packages", nargs="*", default=None)

    fetch_cmd = sub.add_parser("fetch")
    fetch_cmd.add_argument("name")

    refresh_cmd = sub.add_parser("refresh")
    refresh_cmd.add_argument("--limit", type=int, default=None)

    retry_failed_cmd = sub.add_parser("retry-failed")
    retry_failed_cmd.add_argument("--limit", type=int, default=None)

    sub.add_parser("daily-update")

    sub.add_parser("stats")

    registries_cmd = sub.add_parser("registries", help="sources and profiles")
    registries_sub = registries_cmd.add_subparsers(dest="registries_command", required=True)
    import_cmd = registries_sub.add_parser("import", help="create or update sources and profiles from a YAML file")
    import_cmd.add_argument("file")
    test_cmd = registries_sub.add_parser("test", help="check the connection to a source")
    test_cmd.add_argument("slug")
    list_cmd = registries_sub.add_parser("list", help="list sources")
    list_cmd.add_argument("--ecosystem", choices=[e.value for e in Ecosystem], default=None)
    return parser


def main() -> None:
    setup_logging()
    args = build_parser().parse_args()
    settings = get_settings()

    if args.command == "registries":
        sys.exit(_run_registries(args))

    if args.command == "seed":
        packages = args.packages if args.packages else settings.seed_package_list
        result = asyncio.run(_run_seed_async(packages))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "fetch":
        result = asyncio.run(_fetch_package_async(args.name))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "refresh":
        limit = args.limit if args.limit is not None else settings.sync_refresh_batch_limit
        result = asyncio.run(_run_refresh_async(limit=limit))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "retry-failed":
        result = asyncio.run(_run_retry_failed_async(limit=args.limit))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "daily-update":
        from app.tasks.daily_update import _run_daily_update_async

        result = asyncio.run(_run_daily_update_async())
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    if args.command == "stats":
        result = asyncio.run(_stats())
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
