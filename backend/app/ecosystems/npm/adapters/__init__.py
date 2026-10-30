"""Source adapters of the npm plugin: `npm-registry` in `registry`."""

from app.ecosystems.npm.adapters.registry import NpmRegistryAdapter, normalize_dependencies

__all__ = ["NpmRegistryAdapter", "normalize_dependencies"]
