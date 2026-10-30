"""Source adapters of the crates plugin: `cargo-sparse` in `sparse`."""

from app.ecosystems.crates.adapters.sparse import CratesSparseAdapter, enabled_by, normalize_dependencies

__all__ = ["CratesSparseAdapter", "enabled_by", "normalize_dependencies"]
