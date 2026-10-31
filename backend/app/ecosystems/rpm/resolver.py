"""RPM resolve through rpm-worker's libsolv (SPEC-MULTI-ECOSYSTEM.md §9.8); K6."""

from app.ecosystems.base import CatalogAccess, ResolveGraph, ResolveRequest


class RpmResolver:
    emulates = "dnf 4"
    strategy = "libsolv"

    async def resolve(self, request: ResolveRequest, catalog: CatalogAccess) -> ResolveGraph:
        return ResolveGraph(strategy=self.strategy, nodes=[], edges=[], conflicts=[], warnings=[], fetched=[],
                            error={"code": "not_implemented", "message": "RPM resolve arrives with stage E6 K6"})


RESOLVER = RpmResolver()
