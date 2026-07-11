from __future__ import annotations


def has_allowed_role(user_roles: list[str], allowed_roles: list[str]) -> bool:
    return any(role in allowed_roles for role in user_roles)


def permissions_for_roles(roles: list[str]) -> list[str]:
    permissions = ["public"]
    if set(roles) & {"ops", "admin"}:
        permissions.append("internal")
    if "admin" in roles:
        permissions.append("confidential")
    return permissions
