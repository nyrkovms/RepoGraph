from packaging.markers import Marker


def marker_applies(markers: dict | None, env: dict) -> bool:
    if markers is None:
        return True

    expression = markers.get("expression") if isinstance(markers, dict) else None
    if not expression:
        return True

    try:
        return Marker(expression).evaluate(env)
    except Exception:  # noqa: BLE001
        return True
