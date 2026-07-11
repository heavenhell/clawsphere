from __future__ import annotations


ROLE_ORDER = {"readonly": 1, "ops": 2, "admin": 3}


def has_allowed_role(user_roles: list[str], allowed_roles: list[str]) -> bool:
    return any(role in allowed_roles for role in user_roles)


def is_write_intent(text: str) -> bool:
    keywords = ["重启", "停止", "删除", "迁移", "扩容", "修改", "启用", "禁用", "处理掉", "执行"]
    return any(keyword in text for keyword in keywords)


def guarded_message(text: str, roles: list[str]) -> str | None:
    if not is_write_intent(text):
        return None
    if not has_allowed_role(roles, ["ops", "admin"]):
        return "该请求涉及写操作或变更动作，当前 readonly 角色无权执行。我可以继续提供排查建议或生成审批草案。"
    return "该请求涉及高风险变更，demo 环境已拦截实际执行，并生成待审批项。"
