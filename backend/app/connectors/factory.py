"""Connector selection by registry.protocol (D11: protocol != ecosystem)."""

from app.connectors.base import BaseConnector
from app.connectors.npm import NpmConnector
from app.connectors.pypi import PyPIConnector
from app.connectors.pypi_simple import PyPISimpleConnector
from app.fetcher.transport import RegistryInfo

SUPPORTED_PROTOCOLS = ("pypi-json", "pypi-simple", "npm")


class UnsupportedProtocolError(ValueError):
    pass


def registry_info_from_row(registry) -> RegistryInfo:
    """Build the ORM-free connector view from a Registry row."""
    return RegistryInfo(
        slug=registry.slug,
        ecosystem=registry.ecosystem.value if hasattr(registry.ecosystem, "value") else str(registry.ecosystem),
        protocol=registry.protocol,
        base_url=registry.base_url,
        api_url=registry.api_url,
        auth_config=registry.auth_config,
        registry_type=(
            registry.registry_type.value if hasattr(registry.registry_type, "value") else str(registry.registry_type)
        ),
        enumerate_enabled=registry.enumerate_enabled,
    )


def build_connector(registry: RegistryInfo) -> BaseConnector:
    if registry.protocol == "pypi-json":
        return PyPIConnector(registry=registry)
    if registry.protocol == "pypi-simple":
        return PyPISimpleConnector(registry=registry)
    if registry.protocol == "npm":
        return NpmConnector(registry=registry)
    raise UnsupportedProtocolError(
        f"protocol '{registry.protocol}' is not supported; known: {', '.join(SUPPORTED_PROTOCOLS)}"
    )
