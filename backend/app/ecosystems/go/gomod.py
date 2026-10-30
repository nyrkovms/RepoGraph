"""`go.mod` reader (SPEC-MULTI-ECOSYSTEM.md §9.5): what the catalog and the MVS resolver need from the file.

The grammar follows golang.org/x/mod/modfile: directives are `verb args` lines or `verb ( ... )` blocks, `//` starts a
comment, arguments are bare words or "interpreted" / `raw` strings. Versions are kept as written, the version scheme
judges them. Directives this reader has no use for (`godebug`, `tool`, `ignore`, anything newer) are skipped, so a
newer Go file does not break it; a line of a directive it does read that is malformed raises `GoModError`.
"""

import re
from dataclasses import dataclass, field

# Go 1.17 is where module graph pruning starts (SPEC §9.5); a go.mod without a `go` line is read as 1.16.
PRUNING_FROM = (1, 17)
_NO_GO_LINE = (1, 16)
_GO_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?(?:(?:rc|beta|alpha)\d+)?")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}
_PUNCT = frozenset("()[],")


class GoModError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"go.mod line {line}: {message}")
        self.line = line


@dataclass(frozen=True, slots=True)
class Require:
    path: str
    version: str
    indirect: bool = False


@dataclass(frozen=True, slots=True)
class Replace:
    old_path: str
    old_version: str | None
    new_path: str
    new_version: str | None   # None: a directory on disk


@dataclass(frozen=True, slots=True)
class Retract:
    """Versions `low` to `high` inclusive; a single version has low == high."""

    low: str
    high: str
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class GoMod:
    module: str
    deprecated: str | None = None
    go: str | None = None
    toolchain: str | None = None
    requires: list[Require] = field(default_factory=list)
    excludes: list[tuple[str, str]] = field(default_factory=list)
    replaces: list[Replace] = field(default_factory=list)
    retracts: list[Retract] = field(default_factory=list)

    @property
    def pruned(self) -> bool:
        """The module graph is pruned at this module: its dependencies' own go.mod files are not read (§9.5)."""
        match = _GO_VERSION.fullmatch(self.go) if self.go else None
        return (int(match[1]), int(match[2])) >= PRUNING_FROM if match else _NO_GO_LINE >= PRUNING_FROM


# --- lines --------------------------------------------------------------------------------------------------


def _split(line: str, number: int) -> tuple[list[str], str | None]:
    """The tokens of one line and its comment (the text after `//`, trimmed), None if there is none."""
    tokens: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in " \t\r":
            i += 1
        elif line.startswith("//", i):
            return tokens, line[i + 2:].strip()
        elif line.startswith("=>", i):
            tokens.append("=>")
            i += 2
        elif c in _PUNCT:
            tokens.append(c)
            i += 1
        elif c == "`":
            end = line.find("`", i + 1)
            if end < 0:
                raise GoModError(number, "unterminated raw string")
            tokens.append(line[i + 1:end])
            i = end + 1
        elif c == '"':
            text, i = _quoted(line, i, number)
            tokens.append(text)
        else:
            start = i
            while i < n and line[i] not in " \t\r" and line[i] not in _PUNCT and not line.startswith(("//", "=>"), i):
                i += 1
            tokens.append(line[start:i])
    return tokens, None


def _quoted(line: str, start: int, number: int) -> tuple[str, int]:
    out: list[str] = []
    i = start + 1
    while i < len(line):
        c = line[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\":
            i += 1
            if i >= len(line) or line[i] not in _ESCAPES:
                raise GoModError(number, "unsupported escape in a quoted string")
            c = _ESCAPES[line[i]]
        out.append(c)
        i += 1
    raise GoModError(number, "unterminated quoted string")


def _is_indirect(comment: str | None) -> bool:
    return comment is not None and (comment == "indirect" or comment.startswith("indirect;"))


def _deprecation(above: list[str], trailing: str | None) -> str | None:
    """The `Deprecated:` paragraph of the comment above the `module` line, else a comment on the line itself."""
    paragraph: list[str] | None = None
    for text in [*above, ""]:   # the sentinel closes the last paragraph
        if paragraph is None:
            if text.startswith("Deprecated:"):
                paragraph = [text.removeprefix("Deprecated:").strip()]
        elif text:
            paragraph.append(text)
        else:
            break
    if paragraph is not None:
        return " ".join(part for part in paragraph if part) or None
    if trailing is not None and trailing.startswith("Deprecated:"):
        return trailing.removeprefix("Deprecated:").strip() or None
    return None


# --- the file -----------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _State:
    module: str | None = None
    deprecated: str | None = None
    go: str | None = None
    toolchain: str | None = None
    requires: list[Require] = field(default_factory=list)
    excludes: list[tuple[str, str]] = field(default_factory=list)
    replaces: list[Replace] = field(default_factory=list)
    retracts: list[Retract] = field(default_factory=list)


def parse_go_mod(text: str) -> GoMod:
    state = _State()
    block: str | None = None          # the verb of the open `verb (` block
    block_line = 0
    above: list[str] = []             # comment lines directly above the next directive
    for number, raw in enumerate(text.splitlines(), start=1):
        tokens, comment = _split(raw, number)
        if not tokens:
            above = [] if comment is None else [*above, comment]
            continue
        if block is not None:
            if tokens == [")"]:
                block = None
            else:
                _directive(state, block, tokens, comment, above, number)
        elif tokens[-1] == "(" and len(tokens) >= 2:
            if len(tokens) != 2:
                raise GoModError(number, "unexpected tokens before '('")
            block, block_line = tokens[0], number
        elif tokens[0] in _PUNCT:
            raise GoModError(number, f"unexpected {tokens[0]!r}")
        else:
            _directive(state, tokens[0], tokens[1:], comment, above, number)
        above = []
    if block is not None:
        raise GoModError(block_line, f"unterminated {block} block")
    if state.module is None:
        raise GoModError(1, "no module directive")
    return GoMod(
        module=state.module, deprecated=state.deprecated, go=state.go, toolchain=state.toolchain,
        requires=state.requires, excludes=state.excludes, replaces=state.replaces, retracts=state.retracts,
    )


def _directive(state: _State, verb: str, args: list[str], comment: str | None, above: list[str], number: int) -> None:
    if verb == "module":
        if len(args) != 1:
            raise GoModError(number, "module takes one path")
        state.module = args[0]
        state.deprecated = _deprecation(above, comment)
    elif verb == "go":
        if len(args) != 1 or _GO_VERSION.fullmatch(args[0]) is None:
            raise GoModError(number, "go takes one version such as 1.21 or 1.21.3")
        state.go = args[0]
    elif verb == "toolchain":
        if len(args) != 1:
            raise GoModError(number, "toolchain takes one name")
        state.toolchain = args[0]
    elif verb == "require":
        if len(args) != 2:
            raise GoModError(number, "require takes a path and a version")
        state.requires.append(Require(args[0], args[1], _is_indirect(comment)))
    elif verb == "exclude":
        if len(args) != 2:
            raise GoModError(number, "exclude takes a path and a version")
        state.excludes.append((args[0], args[1]))
    elif verb == "replace":
        state.replaces.append(_replace(args, number))
    elif verb == "retract":
        state.retracts.append(_retract(args, comment, above, number))
    # Anything else (godebug, tool, ignore, ...) is not ours.


def _replace(args: list[str], number: int) -> Replace:
    if args.count("=>") != 1:
        raise GoModError(number, "replace needs exactly one '=>'")
    arrow = args.index("=>")
    old, new = args[:arrow], args[arrow + 1:]
    if len(old) not in (1, 2) or len(new) not in (1, 2):
        raise GoModError(number, "replace takes `old [version] => new [version]`")
    if len(new) == 1 and not new[0].startswith(("./", "../", "/")) and re.match(r"[A-Za-z]:[\\/]", new[0]) is None:
        raise GoModError(number, "a replacement without a version must be a directory path")
    return Replace(
        old_path=old[0], old_version=old[1] if len(old) == 2 else None,
        new_path=new[0], new_version=new[1] if len(new) == 2 else None,
    )


def _retract(args: list[str], comment: str | None, above: list[str], number: int) -> Retract:
    rationale = comment or " ".join(above) or None
    if len(args) == 1 and args[0] not in _PUNCT:
        return Retract(args[0], args[0], rationale)
    if len(args) == 5 and args[0] == "[" and args[2] == "," and args[4] == "]":
        return Retract(args[1], args[3], rationale)
    raise GoModError(number, "retract takes a version or `[low, high]`")
