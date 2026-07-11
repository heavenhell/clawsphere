from __future__ import annotations


def has_allowed_role(user_roles: list[str], allowed_roles: list[str]) -> bool:
    return any(role in allowed_roles for role in user_roles)
