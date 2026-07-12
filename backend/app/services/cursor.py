import base64
import json
import uuid
from datetime import datetime
from typing import Any


class CursorError(ValueError):
    pass


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise CursorError("invalid cursor") from exc
    if not isinstance(data, dict):
        raise CursorError("invalid cursor payload")
    return data


def parse_iso_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise CursorError("invalid datetime in cursor") from exc


def parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise CursorError("invalid uuid in cursor") from exc
