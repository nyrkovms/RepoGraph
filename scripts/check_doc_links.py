#!/usr/bin/env python3
"""Check that relative links in *.md files point to existing files (stdlib only).

Run from anywhere: `python3 -I scripts/check_doc_links.py` (or `make docs-check`).
"""

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
SKIP_PATHS = {"frontend/dist", ".claude/worktrees"}
SKIP_DIR_NAMES = {"node_modules", ".venv", ".git"}
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")
ABSOLUTE_PREFIXES = ("/Users/", "/home/")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


def skipped(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    rel_posix = rel.as_posix()
    if any(rel_posix == s or rel_posix.startswith(s + "/") for s in SKIP_PATHS):
        return True
    if SKIP_DIR_NAMES.intersection(rel.parts):
        return True
    # Hidden directories are not docs, except .claude (agents and templates are markdown too).
    return any(part.startswith(".") and part != ".claude" for part in rel.parts[:-1])


def problems_in(path: Path) -> list[str]:
    found = []
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for target in LINK.findall(line):
            if target.startswith(EXTERNAL_PREFIXES) or target.startswith("#"):
                continue
            where = f"{path.relative_to(ROOT)}:{number} -> {target}"
            if target.startswith(ABSOLUTE_PREFIXES):
                found.append(f"{where} (absolute filesystem link)")
                continue
            file_part = unquote(target.split("#", 1)[0])
            if not file_part:
                continue
            resolved = ROOT / file_part.lstrip("/") if file_part.startswith("/") else path.parent / file_part
            if not resolved.exists():
                found.append(where)
    return found


def main() -> int:
    problems = []
    for path in sorted(ROOT.rglob("*.md")):
        if not skipped(path):
            problems.extend(problems_in(path))
    for problem in problems:
        print(problem)
    print(f"{len(problems)} broken link(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
