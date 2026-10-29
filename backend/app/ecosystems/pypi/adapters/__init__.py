"""Source adapters of the PyPI plugin: `pypi-warehouse` (pypi.org) in `warehouse`, `pypi-simple` in `simple`."""

from app.ecosystems.pypi.adapters.simple import PyPISimpleAdapter
from app.ecosystems.pypi.adapters.warehouse import (
    WarehouseAdapter,
    dependencies_from_requires_dist,
    to_contract,
    warehouse_urls,
)

__all__ = ["PyPISimpleAdapter", "WarehouseAdapter", "dependencies_from_requires_dist", "to_contract", "warehouse_urls"]
