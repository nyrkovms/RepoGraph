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

    classify_cmd = sub.add_parser("classify")
    classify_cmd.add_argument("--reclassify", action="store_true")
    classify_cmd.add_argument("--limit", type=int, default=None)

    export_ann_cmd = sub.add_parser("export-annotations")
    export_ann_cmd.add_argument("--output", default="annotations.jsonl")

    import_ann_cmd = sub.add_parser("import-annotations")
    import_ann_cmd.add_argument("--input", required=True)
    import_ann_cmd.add_argument("--replace", action="store_true")

    export_llm_cmd = sub.add_parser("export-llm-input")
    export_llm_cmd.add_argument("--output", default="llm_input.jsonl")
    export_llm_cmd.add_argument("--max-chars", type=int, default=8000)

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

    if args.command == "classify":
        from app.tasks.classify import _run_classification_async

        result = asyncio.run(_run_classification_async(reclassify=args.reclassify, limit=args.limit))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command in ("export-annotations", "import-annotations", "export-llm-input"):
        from pathlib import Path

        from app.classifier.export import export_annotations, export_llm_input, import_annotations

        async def _run_export() -> dict:
            async with SessionLocal() as db:
                if args.command == "export-annotations":
                    count = await export_annotations(db, Path(args.output))
                    return {"exported": count, "output": args.output}
                if args.command == "import-annotations":
                    return await import_annotations(db, Path(args.input), replace=args.replace)
                count = await export_llm_input(db, Path(args.output), max_chars=args.max_chars)
                return {"exported": count, "output": args.output}

        print(json.dumps(asyncio.run(_run_export()), ensure_ascii=False, indent=2))
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
