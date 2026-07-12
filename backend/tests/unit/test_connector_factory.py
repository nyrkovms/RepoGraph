import pytest

from app.connectors.factory import UnsupportedProtocolError, build_connector
from app.connectors.pypi import PyPIConnector
from app.connectors.pypi_simple import PyPISimpleConnector
from app.fetcher.transport import RegistryInfo


def info(protocol: str) -> RegistryInfo:
    return RegistryInfo(
        slug="r",
        ecosystem="pypi",
        protocol=protocol,
        base_url="https://registry.example/simple/",
    )


def test_dispatch_pypi_json():
    connector = build_connector(info("pypi-json"))
    assert isinstance(connector, PyPIConnector)
    assert connector.registry.slug == "r"


def test_dispatch_pypi_simple():
    connector = build_connector(info("pypi-simple"))
    assert isinstance(connector, PyPISimpleConnector)
    assert connector.index_base_url == "https://registry.example/simple"


def test_unknown_protocol_rejected():
    with pytest.raises(UnsupportedProtocolError):
        build_connector(info("goproxy"))
