"""The purl of a catalog package, for API responses."""

from typing import cast

from app.ecosystems.base import Ecosystem, PackageRef
from app.ecosystems.purl import Purl, purl_type_for
from app.ecosystems.registry import UnknownEcosystem, get_plugin, load_builtin_plugins


def package_purl(ecosystem: str, namespace: str | None, normalized_name: str) -> str:
    """Through the ecosystem's plugin when there is one; otherwise from the generic grammar, so that rows of an
    ecosystem whose plugin is not written yet still get a purl."""
    load_builtin_plugins()
    try:
        plugin = get_plugin(ecosystem)
    except UnknownEcosystem:
        return Purl(type=purl_type_for(ecosystem), namespace=namespace or None, name=normalized_name).to_string()
    ref = PackageRef(ecosystem=cast(Ecosystem, ecosystem), namespace=namespace or "", name=normalized_name)
    return plugin.coordinates.to_purl(ref)
