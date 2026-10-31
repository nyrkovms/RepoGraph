"""Plugin registry: the rest of the application reaches ecosystem code only through get_plugin()."""

import importlib

from app.ecosystems.base import Ecosystem, EcosystemPlugin


class UnknownEcosystem(LookupError):
    pass


_PLUGINS: dict[str, EcosystemPlugin] = {}


def register(plugin: EcosystemPlugin) -> None:
    if plugin.ecosystem in _PLUGINS:
        raise ValueError(f"plugin for {plugin.ecosystem!r} is already registered")
    _PLUGINS[plugin.ecosystem] = plugin


def get_plugin(ecosystem: Ecosystem | str) -> EcosystemPlugin:
    """The plugin of the ecosystem. Built-in plugins are loaded on first use: a fresh process (the api right after a
    restart, a CLI command) must not depend on some other code path having imported them first."""
    if ecosystem not in _PLUGINS:
        load_builtin_plugins()
    try:
        return _PLUGINS[ecosystem]
    except KeyError:
        raise UnknownEcosystem(ecosystem) from None


def registered() -> list[EcosystemPlugin]:
    _import_builtins()
    return [_PLUGINS[name] for name in sorted(_PLUGINS)]


# Modules whose import registers a built-in plugin; each stage adds its ecosystem (E0-A6: pypi, E1: npm).
BUILTIN_PLUGIN_MODULES: tuple[str, ...] = (
    "app.ecosystems.pypi.plugin", "app.ecosystems.npm.plugin", "app.ecosystems.crates.plugin", "app.ecosystems.go.plugin",
    "app.ecosystems.nuget.plugin", "app.ecosystems.maven.plugin", "app.ecosystems.rpm.plugin",
)


def load_builtin_plugins() -> list[EcosystemPlugin]:
    return registered()


def _import_builtins() -> None:
    for module in BUILTIN_PLUGIN_MODULES:
        importlib.import_module(module)   # registers the plugin on first import; a no-op afterwards
