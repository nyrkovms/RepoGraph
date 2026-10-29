from fastapi.testclient import TestClient


def test_lazy_fetch_survives_consecutive_resolves(client: TestClient) -> None:
    # #6: lazy fetch from the resolver thread once ran a new event loop per missing package, and a connection pooled
    # by one loop was handed to the next ("attached to a different loop", segfaults in asyncpg). Catalog calls now go
    # back to the request's loop; consecutive resolves that fetch packages must all succeed.
    for name in ("tzdata", "idna", "tzdata"):
        response = client.post("/api/v1/resolve", json={"roots": [{"purl": f"pkg:pypi/{name}"}]})
        assert response.status_code == 200
        graph = response.json()
        assert graph["error"] is None, graph["error"]
        assert [n["name"] for n in graph["nodes"]] == [name]
