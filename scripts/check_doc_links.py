#!/usr/bin/env python3
"""Check that relative links in *.md files point to existing files (stdlib only).

Run from anywhere: `python3 -I scripts/check_doc_links.py` (or `make docs-check`).
Inline links `[text](path)` and `[text](<path>)` and reference definitions `[x]: path` are checked; fenced code and
inline code are skipped, external links and anchors are not checked.
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
SKIP_PATHS = {"frontend/dist", ".claude/worktrees"}
SKIP_DIR_NAMES = {"node_modules", ".venv", ".git"}
# Hidden directories are not docs, except these (agents, templates, workflow docs are markdown too).
HIDDEN_KEPT = {".claude", ".github"}
INLINE_CODE = re.compile(r"(`+).*?\1")
LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))")
FENCE = re.compile(r"^\s*(```|~~~)")
ABSOLUTE_PREFIXES = ("/Users/", "/home/")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


def markdown_files() -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = Path(dirpath).relative_to(ROOT).as_posix()
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIR_NAMES
            and (not d.startswith(".") or d in HIDDEN_KEPT)
            and (f"{rel}/{d}" if rel != "." else d) not in SKIP_PATHS
        ]
        found.extend(Path(dirpath) / name for name in filenames if name.endswith(".md"))
    return sorted(found)


def targets(line: str) -> list[str]:
    reference = REFERENCE.match(line)
    if reference:
        return [reference.group(1) or reference.group(2)]
    line = INLINE_CODE.sub("", line)
    return [angle or bare for angle, bare in LINK.findall(line)]


def problems_in(path: Path) -> list[str]:
    found = []
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for target in targets(line):
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
    for path in markdown_files():
        problems.extend(problems_in(path))
    for problem in problems:
        print(problem)
    print(f"{len(problems)} broken link(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
