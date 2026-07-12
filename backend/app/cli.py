import argparse
import asyncio
import json

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.logging_config import setup_logging
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
        dependencies = await db.scalar(select(func.count(Dependency.id)))

    return {
        "packages": int(packages or 0),
        "versions": int(versions or 0),
        "dependencies": int(dependencies or 0),
    }


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_cmd = sub.add_parser("seed")
    seed_cmd.add_argument("--packages", nargs="*", default=None)

    fetch_cmd = sub.add_parser("fetch")
    fetch_cmd.add_argument("name")
    fetch_cmd.add_argument("--ecosystem", choices=("pypi", "npm"), default="pypi")

    refresh_cmd = sub.add_parser("refresh")
    refresh_cmd.add_argument("--limit", type=int, default=None)

    retry_failed_cmd = sub.add_parser("retry-failed")
    retry_failed_cmd.add_argument("--limit", type=int, default=None)

    sub.add_parser("stats")

    args = parser.parse_args()
    settings = get_settings()

    if args.command == "seed":
        packages = args.packages if args.packages else settings.seed_package_list
        result = asyncio.run(_run_seed_async(packages))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "fetch":
        result = asyncio.run(_fetch_package_async(args.name, args.ecosystem))
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

    if args.command == "stats":
        result = asyncio.run(_stats())
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
