from __future__ import annotations

from typing import Any, Iterable


def coalesce(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def extract_items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in (*keys, "items", "objList", "data", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_items(value, *keys)
            if nested:
                return nested
    return []


def resource_id(data: dict[str, Any], *keys: str) -> str:
    value = coalesce(data, *keys, "id", "urn", "pid", default="")
    text = str(value)
    return text.rsplit(":", 1)[-1] if text else ""


def ratio(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(number / 100, 4) if number > 1 else number


def total(values: Iterable[Any]) -> float:
    result = 0.0
    for value in values:
        try:
            result += float(value or 0)
        except (TypeError, ValueError):
            continue
    return result
