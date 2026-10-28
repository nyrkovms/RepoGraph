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
    try:
        return _PLUGINS[ecosystem]
    except KeyError:
        raise UnknownEcosystem(ecosystem) from None


def registered() -> list[EcosystemPlugin]:
    return [_PLUGINS[name] for name in sorted(_PLUGINS)]


# Modules whose import registers a built-in plugin; each stage adds its ecosystem (E0-A6: pypi).
BUILTIN_PLUGIN_MODULES: tuple[str, ...] = ()


def load_builtin_plugins() -> list[EcosystemPlugin]:
    for module in BUILTIN_PLUGIN_MODULES:
        importlib.import_module(module)
    return registered()
