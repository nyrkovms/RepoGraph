"""Platform conditions of Cargo dependencies (`[target.'cfg(...)'.dependencies]`, SPEC-MULTI-ECOSYSTEM.md §7.3, §9.4).

A dependency's `target` is either a target triple (`x86_64-pc-windows-gnu`) or a `cfg(...)` expression, the grammar of
the `cargo-platform` crate: `all(...)`, `any(...)`, `not(...)`, a bare name (`unix`) or `key = "value"`. It is evaluated
against the cfg set rustc reports for the target (`targets.json`, scripts/generate_rust_targets.sh). Names that are not
in the set are false, as Cargo has them without `--cfg` (`tokio_unstable`).
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_TARGETS_FILE = Path(__file__).with_name("targets.json")
_TOKEN = re.compile(r'\s*(?:([A-Za-z_][A-Za-z0-9_]*)|("[^"]*")|([(),=]))')


class InvalidCfg(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CfgSet:
    names: frozenset[str]
    pairs: frozenset[tuple[str, str]]


@lru_cache(maxsize=1)
def _targets() -> dict[str, CfgSet]:
    raw: dict[str, list[str]] = json.loads(_TARGETS_FILE.read_text())["targets"]
    out: dict[str, CfgSet] = {}
    for target, lines in raw.items():
        names = frozenset(line for line in lines if "=" not in line)
        pairs = frozenset((key, value.strip('"')) for key, _, value in (line.partition("=") for line in lines if "=" in line))
        out[target] = CfgSet(names, pairs)
    return out


def known_targets() -> list[str]:
    return sorted(_targets())


def cfg_of(target: str) -> CfgSet:
    try:
        return _targets()[target]
    except KeyError:
        raise ValueError(f"unknown Rust target {target!r}; known: {', '.join(known_targets())}") from None


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise InvalidCfg(f"cannot read {text!r} at {position}")
        tokens.append(match.group(match.lastindex or 0))
        position = match.end()
    return tokens


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = _tokens(text)
        self.position = 0

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self._peek()
        if token is None or (expected is not None and token != expected):
            raise InvalidCfg(f"{self.text!r}: expected {expected or 'more'}, got {token!r}")
        self.position += 1
        return token

    def expression(self) -> tuple[object, ...]:
        name = self._take()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise InvalidCfg(f"{self.text!r}: {name!r} is not a name")
        if name in ("all", "any", "not") and self._peek() == "(":
            self._take("(")
            items: list[tuple[object, ...]] = []
            while self._peek() != ")":
                items.append(self.expression())
                if self._peek() != ",":
                    break
                self._take(",")
            self._take(")")
            if name == "not" and len(items) != 1:
                raise InvalidCfg(f"{self.text!r}: not() takes one expression")
            return (name, *items)
        if self._peek() == "=":
            self._take("=")
            value = self._take()
            if not value.startswith('"'):
                raise InvalidCfg(f"{self.text!r}: expected a string after {name} =")
            return ("pair", name, value[1:-1])
        return ("name", name)

    def whole(self) -> tuple[object, ...]:
        expression = self.expression()
        if self._peek() is not None:
            raise InvalidCfg(f"{self.text!r}: unexpected {self._peek()!r}")
        return expression


@lru_cache(maxsize=4096)
def parse_platform(spec: str) -> tuple[object, ...]:
    """`("triple", name)` or the parsed `cfg(...)` expression; raises InvalidCfg."""
    text = spec.strip()
    if text.startswith("cfg(") and text.endswith(")"):
        return _Parser(text[4:-1]).whole()
    if re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return ("triple", text)
    raise InvalidCfg(f"{spec!r} is neither a target nor cfg(...)")


def _evaluate(node: tuple[object, ...], cfg: CfgSet) -> bool:
    kind = node[0]
    if kind == "name":
        return node[1] in cfg.names
    if kind == "pair":
        return (node[1], node[2]) in cfg.pairs
    items = [item for item in node[1:] if isinstance(item, tuple)]
    if kind == "all":
        return all(_evaluate(item, cfg) for item in items)
    if kind == "any":
        return any(_evaluate(item, cfg) for item in items)
    return not _evaluate(items[0], cfg)


def platform_matches(spec: str | None, target: str) -> bool:
    """A dependency with this `target` applies on `target`. An expression that cannot be read applies nowhere."""
    if not spec:
        return True
    try:
        parsed = parse_platform(spec)
    except InvalidCfg:
        return False
    if parsed[0] == "triple":
        return parsed[1] == target
    return _evaluate(parsed, cfg_of(target))
