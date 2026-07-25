from __future__ import annotations

import json
import threading
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from backend.agent.llm import (
    DEEPSEEK_MODEL,
    call_deepseek_agent_plan,
    call_deepseek_json,
    get_llm_status,
    get_public_llm_status,
    reset_llm_request_status,
    summarize_messages,
)
from backend.agent.checkpoint import close_checkpointer, get_checkpointer
from backend.agent.identity import build_system_prompt
from backend.guardrails.approvals import approval_store
from backend.guardrails.policy import risk_for_tool, validate_tool_calls
from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.memory.context_manager import (
    RESOURCE_ID_PATTERN,
    deterministic_summary,
    manage_context_window,
)
from backend.memory.database import memory_db
from backend.memory.retriever import retrieve, retrieve_history
from backend.memory.store import write_conversation_summary
from backend.skills.loader import startup_skill_summaries
from backend.observability import observe_agent


# --- Configuration -----------------------------------------------------------
# Death-loop / runaway guardrail: a single turn may execute at most this many
# tool calls, no matter what the model proposes.
MAX_TOOL_CALLS_PER_TURN = 8


class CopilotState(TypedDict, total=False):
    messages: list[dict[str, str]]
    message: str
    intent: str
    task_id: str
    conversation_id: str
    user_id: str
    user_roles: list[str]
    tenant_id: str
    conversation_summary: str
    recent_messages: list[dict[str, str]]
    relevant_messages: list[dict[str, str]]
    working_context: dict[str, Any]
    context_metrics: dict[str, Any]
    alert_payload: dict | None
    resource_snapshot: dict | None
    retrieved_docs: list[dict]
    retrieved_history: list[dict]
    plan: list[str]
    plan_source: str
    tool_calls_proposed: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    hitl_required: bool
    hitl_approved: bool | None
    execution_log: list[dict[str, Any]]
    final_response: str
    resource_claims: list[dict[str, Any]]
    response_source: str
    llm_available: bool
    step_budget_hit: bool
    llm_status: dict[str, Any]
    fallback_reason: str | None
    summary: str
    error: str | None


SYSTEM_PROMPT = build_system_prompt(DEEPSEEK_MODEL, startup_skill_summaries())

ROUTER_PROMPT = f"""你是 DCS/FusionCompute/eDME 运维 Copilot 的路由器。
根据完整对话判断用户意图,自行决定调用哪些只读工具获取真实数据,或不调用工具直接回答概念/寒暄/身份类问题。
规则:
- 优先使用最少、最相关的工具。一般的告警/资源/容量/性能问题使用 FusionCompute/Dorado 工具(如 list_alarms、get_resource_overview);只有用户明确提到 eDME、OceanStor 或存储设备纳管时才使用 query_edme_* 工具。
- 调用工具时严格按参数 schema 传参,不要添加 schema 未定义的字段,不确定的可选参数就不要传。
- 只有用户明确指向的对象(VM/集群/告警/存储 ID 或名称)才调用相关工具;缺少标识时不要猜测资源,也不要调用工具。
- 术语解释、概念说明、寒暄、自我介绍等不需要实时数据的问题,不要调用工具。
- 写操作(重启/扩容/迁移/修改/删除)只提出对应工具调用,是否执行由护栏和审批独立裁决,你无法绕过。
- 追问("它呢""第二条""上面说的X")请结合历史自行消解指代。

可用 Skill 第一层:
{startup_skill_summaries()}"""

RESPONDER_PROMPT = """你是 DCS/FusionCompute/eDME 运维 Copilot,负责生成最终回答。
必须严格输出 JSON,字段:
- "answer": 给用户的自然语言回答。结论优先,附证据、建议动作、风险/下一步。
- "resource_claims": 数组。回答里出现的每一个资源 ID(如 vm-1001、host-005、alarm-9001、edme-storage-002)都必须在此申报一条:
    - {"id": "<资源ID>", "kind": "example"}  用于举例说明或引用历史上下文,不断言其当前状态。
    - {"id": "<资源ID>", "kind": "state_assertion", "from_tool": "<工具名>"}  断言该资源的当前状态/数值,必须来自本轮某个工具结果。

硬性要求:
- 只能基于 tool_results 里的真实数据断言资源状态;严禁编造 tool_results 之外的资源、数值或状态。
- 解释术语/概念时只讲原理,可引用历史对象举例(kind=example),但不要声称它们的当前状态。
- 写操作在审批前一律说明"已进入审批,未执行",不得声称已完成。
- answer 中提到的每个资源 ID 都必须在 resource_claims 里出现,不得遗漏。"""

LLM_UNAVAILABLE_MESSAGE = (
    "当前未配置或无法连接大模型(DeepSeek)。本系统的意图理解与回答生成依赖大模型,"
    "请在 .env 中配置 DEEPSEEK_API_KEY 或恢复网络后重试。"
)


def _rbac_tool_catalog(roles: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_model.model_json_schema(),
            },
        }
        for spec in TOOL_REGISTRY.values()
        if any(role in spec.auth_roles for role in roles)
    ]


def _llm_history(state: CopilotState) -> list[dict[str, str]]:
    """Compose the chat history handed to the LLM: rolling summary + relevant +
    recent turns + the current user message."""
    history: list[dict[str, str]] = []
    summary = state.get("conversation_summary", "").strip()
    if summary:
        history.append({"role": "system", "content": f"对话摘要:\n{summary}"})
    seen: set[tuple[str, str]] = set()
    for item in [*state.get("relevant_messages", []), *state.get("recent_messages", [])]:
        key = (item.get("role", ""), item.get("content", ""))
        if key in seen:
            continue
        seen.add(key)
        history.append({"role": item.get("role", "user"), "content": item.get("content", "")})
    history.append({"role": "user", "content": state["message"]})
    return history


# --- Context / retrieval (feed the model) ------------------------------------

def context_loader(state: CopilotState) -> CopilotState:
    def summarizer(messages: list[dict[str, str]]) -> str:
        try:
            return summarize_messages(messages) or deterministic_summary(messages)
        except Exception:
            return deterministic_summary(messages)

    context = manage_context_window(
        state.get("messages", []),
        state.get("conversation_summary", ""),
        summarizer,
        current_message=state["message"],
    )
    return {
        "recent_messages": context["recent_messages"],
        "relevant_messages": context["relevant_messages"],
        "working_context": context["working_context"],
        "context_metrics": {
            "schema_version": context["schema_version"],
            "older_message_count": context["older_message_count"],
            "relevant_message_count": len(context["relevant_messages"]),
            "estimated_tokens": context["estimated_tokens"],
        },
        "conversation_summary": context["conversation_summary"],
        "resource_snapshot": None,
        "alert_payload": None,
    }


def memory_retriever(state: CopilotState) -> CopilotState:
    docs = retrieve(state["message"], state["user_roles"], top_k=3, tenant_id=state["tenant_id"], tier=2)
    history = retrieve_history(state["message"], state["user_roles"], top_k=2, tenant_id=state["tenant_id"])
    return {"retrieved_docs": docs, "retrieved_history": history}


# --- Cognitive layer: routing is the model's job -----------------------------

def llm_router(state: CopilotState) -> CopilotState:
    tools = _rbac_tool_catalog(state["user_roles"])
    try:
        plan = call_deepseek_agent_plan(ROUTER_PROMPT, _llm_history(state), tools)
    except Exception:
        plan = None
    if plan is None:
        return {
            "tool_calls_proposed": [],
            "plan": [],
            "plan_source": "llm_unavailable",
            "llm_available": False,
            "step_budget_hit": False,
            "intent": "unavailable",
        }
    calls = plan.get("tool_calls", []) or []
    step_budget_hit = len(calls) > MAX_TOOL_CALLS_PER_TURN
    calls = calls[:MAX_TOOL_CALLS_PER_TURN]
    reason = plan.get("reason") or ""
    return {
        "tool_calls_proposed": calls,
        "plan": [reason] if reason else [],
        "plan_source": "deepseek_agent",
        "llm_available": True,
        "step_budget_hit": step_budget_hit,
        "intent": "tool_execution" if calls else "direct_answer",
    }


# --- Safety spine (unchanged): RBAC / risk / rate limit / HITL / execute -----

def guardrail(state: CopilotState) -> CopilotState:
    result = validate_tool_calls(
        state.get("tool_calls_proposed", []),
        state["user_roles"],
        state["message"],
        state["task_id"],
    )
    if not result["allowed"]:
        return {
            "tool_calls_proposed": [],
            "hitl_required": False,
            "hitl_approved": False,
            "error": "；".join(result["violations"]),
        }
    return {
        "tool_calls_proposed": result["tool_calls"],
        "hitl_required": result["hitl_required"],
        "hitl_approved": None if result["hitl_required"] else True,
    }


def hitl_interrupt(state: CopilotState) -> CopilotState:
    # LangGraph reruns this node from the top after resume. Keep every operation
    # before interrupt() idempotent; create_or_get is keyed by task_id for that reason.
    risk_order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    approval_risk = max(
        (risk_for_tool(call["tool_name"]) for call in state.get("tool_calls_proposed", [])),
        key=lambda risk: risk_order.get(risk, 0),
        default="medium",
    )
    item = approval_store.create_or_get(
        state["task_id"],
        state["conversation_id"],
        state["user_id"],
        state["tenant_id"],
        state["message"],
        state.get("tool_calls_proposed", []),
        approval_risk,
    )
    decision = interrupt({
        "approval_id": item["id"],
        "task_id": item["task_id"],
        "description": item["description"],
        "tool_calls": item["tool_calls"],
        "risk": item["risk"],
    })
    approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
    approver = decision.get("approver", "unknown") if isinstance(decision, dict) else "unknown"
    reason = decision.get("reason", "") if isinstance(decision, dict) else ""
    approval_store.decide(item["id"], approved, approver, reason)
    if approved:
        return {"hitl_approved": True}
    return {
        "hitl_approved": False,
        "tool_calls_proposed": [],
        "final_response": f"审批 {item['id']} 已拒绝，变更未执行。",
    }


def tool_executor(state: CopilotState) -> CopilotState:
    results = []
    execution_log = []
    calls = state.get("tool_calls_proposed", [])
    write_calls = [call for call in calls if risk_for_tool(call["tool_name"]) in {"medium", "high"}]
    if write_calls:
        recheck = validate_tool_calls(write_calls, state["user_roles"], state["message"], state["task_id"])
        if not recheck["allowed"]:
            return {
                "tool_results": [],
                "execution_log": [],
                "error": "执行前复检失败：" + "；".join(recheck["violations"]),
            }
    for call in calls:
        response = call_tool(ToolRequest(
            tool_name=call["tool_name"],
            params=call["params"],
            caller_roles=state["user_roles"],
            caller_user_id=state["user_id"],
            tenant_id=state["tenant_id"],
            task_id=state["task_id"],
        ))
        result = response.model_dump()
        results.append(result)
        execution_log.append({"tool_name": call["tool_name"], "success": response.success, "audit_id": response.audit_id})
    return {"tool_results": results, "execution_log": execution_log}


# --- Response + verification: structured output, code-side grounding ---------

def _collect_ids(text: str) -> set[str]:
    return {item.lower() for item in RESOURCE_ID_PATTERN.findall(text)}


def verify_resource_claims(
    answer: str,
    claims: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Deterministic grounding check over the model's self-declared claims.

    Every resource ID mentioned in the answer must be either:
      - present in this turn's tool results (grounded by real data), or
      - explicitly declared as an `example` (concept illustration / history).
    Anything else is a fabrication or an ungrounded history-state claim and is
    rejected. State-assertion claims must additionally be backed by tool data."""
    answer_ids = _collect_ids(answer)
    # Text of this turn's tool results — substring lookup grounds any ID format
    # the tools actually returned (site-001, pool ids, hex ids), not only those
    # matched by the resource-ID pattern.
    tool_text = json.dumps(tool_results, ensure_ascii=False).lower()
    tool_ids = _collect_ids(json.dumps(tool_results, ensure_ascii=False))
    example_ids = {
        str(c.get("id", "")).lower()
        for c in claims
        if c.get("kind") == "example" and c.get("id")
    }
    for answer_id in answer_ids:
        if answer_id in tool_ids or answer_id in example_ids:
            continue
        return False, f"回答中出现无数据支撑的资源引用：{answer_id}"
    for claim in claims:
        if claim.get("kind") == "state_assertion":
            claim_id = str(claim.get("id", "")).lower()
            if claim_id and claim_id not in tool_text:
                return False, f"状态断言无工具数据支撑：{claim.get('id')}"
    return True, ""


RESPONDER_TOOL_DATA_CAP = 8000
RESPONDER_PAYLOAD_CAP = 48_000


def _cap(value: Any, limit: int) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return value
    return text[:limit] + "…(截断)"


def _responder_payload(state: CopilotState, feedback: str = "") -> str:
    """Bounded payload for the responder. The full tool_results can exceed the
    outbound size cap, so each result's data is capped and low-value context is
    trimmed. Resource IDs are preserved so grounding stays meaningful."""
    tool_results = [
        {
            "tool_name": item.get("tool_name"),
            "success": item.get("success"),
            "error_code": item.get("error_code"),
            "data": _cap(item.get("data"), RESPONDER_TOOL_DATA_CAP),
        }
        for item in state.get("tool_results", [])
    ]
    payload = {
        "message": state["message"],
        "conversation_summary": state.get("conversation_summary", "")[:1500],
        "working_context": state.get("working_context", {}),
        "recent_messages": state.get("recent_messages", [])[-6:],
        "retrieved_docs": [
            {"title": doc.get("title"), "content": (doc.get("content") or "")[:500]}
            for doc in state.get("retrieved_docs", [])[:3]
        ],
        "plan": state.get("plan", []),
        "tool_results": tool_results,
        "error": state.get("error"),
    }
    if feedback:
        payload["previous_attempt_rejected_because"] = feedback
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(serialized.encode("utf-8")) > RESPONDER_PAYLOAD_CAP:
        # Last-resort shrink: drop retrieved docs, then hard-truncate.
        payload["retrieved_docs"] = []
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(serialized.encode("utf-8")) > RESPONDER_PAYLOAD_CAP:
            serialized = serialized[:RESPONDER_PAYLOAD_CAP] + "…(截断)"
    return serialized


def llm_responder(state: CopilotState) -> CopilotState:
    if state.get("final_response"):
        # Set upstream (e.g. HITL rejection message).
        return {"response_source": "policy", "llm_status": get_public_llm_status()}
    if state.get("llm_available") is False:
        return {
            "final_response": LLM_UNAVAILABLE_MESSAGE,
            "response_source": "unavailable",
            "llm_status": get_public_llm_status(),
            "fallback_reason": "llm_not_configured",
        }
    if state.get("error"):
        return {
            "final_response": f"请求被护栏拦截：{state['error']}",
            "response_source": "guardrail",
            "llm_status": get_public_llm_status(),
            "fallback_reason": "guardrail_block",
        }

    feedback = ""
    last_reason = "grounding_rejected"
    for attempt in range(3):
        try:
            # Generous token budget: the model must emit the answer plus a claim
            # per resource as JSON; too small a cap truncates the JSON (finish
            # reason "length") and it fails to parse.
            parsed = call_deepseek_json(RESPONDER_PROMPT, _responder_payload(state, feedback), max_tokens=4096)
        except Exception as exc:
            # Transient/endpoint error: retry within budget before degrading.
            last_reason = f"llm_error:{type(exc).__name__}"
            if attempt < 2:
                continue
            return {
                "final_response": LLM_UNAVAILABLE_MESSAGE,
                "response_source": "unavailable",
                "llm_status": get_public_llm_status(),
                "fallback_reason": last_reason,
            }
        if not parsed or not isinstance(parsed.get("answer"), str) or not parsed["answer"].strip():
            feedback = "上一轮输出不是符合 schema 的 JSON,请严格输出 answer 与 resource_claims 字段。"
            last_reason = "schema_invalid"
            continue
        answer = parsed["answer"].strip()
        claims = parsed.get("resource_claims") or []
        if not isinstance(claims, list):
            claims = []
        grounded, reason = verify_resource_claims(answer, claims, state.get("tool_results", []))
        if grounded:
            return {
                "final_response": answer,
                "resource_claims": claims,
                "response_source": "deepseek",
                "llm_status": get_public_llm_status(),
                "fallback_reason": None,
            }
        feedback = reason
        last_reason = "grounding_rejected"

    return {
        "final_response": (
            "抱歉,我无法基于当前已核实的数据给出可靠回答,以免提供未经证实的资源状态。"
            "请补充明确的资源标识或稍后重试。"
        ),
        "response_source": "grounding_guard",
        "llm_status": get_public_llm_status(),
        "fallback_reason": last_reason,
    }


def memory_writer(state: CopilotState) -> CopilotState:
    turn_summary = f"message={state.get('message')[:120]}; tools={[r.get('tool_name') for r in state.get('tool_results', [])]}"
    write_conversation_summary(
        state["task_id"],
        state["user_id"],
        state["tenant_id"],
        turn_summary,
        state.get("execution_log", []),
    )
    memory_db.append_turn(
        state["conversation_id"],
        state["user_id"],
        state["tenant_id"],
        state["message"],
        state.get("final_response", ""),
        state.get("conversation_summary", ""),
    )
    return {"summary": turn_summary}


def error_handler(state: CopilotState) -> CopilotState:
    return {"final_response": f"请求处理失败：{state.get('error')}"}


def route_after_guardrail(state: CopilotState) -> Literal["hitl_interrupt", "tool_executor", "llm_responder"]:
    if state.get("error"):
        return "llm_responder"
    if state.get("hitl_required"):
        return "hitl_interrupt"
    if state.get("tool_calls_proposed"):
        return "tool_executor"
    return "llm_responder"


def route_after_hitl(state: CopilotState) -> Literal["tool_executor", "llm_responder"]:
    return "tool_executor" if state.get("hitl_approved") else "llm_responder"


def build_graph():
    builder = StateGraph(CopilotState)
    for name, fn in [
        ("context_loader", context_loader),
        ("memory_retriever", memory_retriever),
        ("llm_router", llm_router),
        ("guardrail", guardrail),
        ("hitl_interrupt", hitl_interrupt),
        ("tool_executor", tool_executor),
        ("llm_responder", llm_responder),
        ("memory_writer", memory_writer),
        ("error_handler", error_handler),
    ]:
        builder.add_node(name, fn)
    builder.set_entry_point("context_loader")
    builder.add_edge("context_loader", "memory_retriever")
    builder.add_edge("memory_retriever", "llm_router")
    builder.add_edge("llm_router", "guardrail")
    builder.add_conditional_edges("guardrail", route_after_guardrail)
    builder.add_conditional_edges("hitl_interrupt", route_after_hitl)
    builder.add_edge("tool_executor", "llm_responder")
    builder.add_edge("llm_responder", "memory_writer")
    builder.add_edge("memory_writer", END)
    return builder.compile(checkpointer=get_checkpointer())


_graph = None
_graph_lock = threading.Lock()
_CONVERSATION_LOCKS = tuple(threading.RLock() for _ in range(64))


class PendingApprovalError(RuntimeError):
    def __init__(self, approval_id: str):
        self.approval_id = approval_id
        super().__init__(f"会话存在待审批任务 {approval_id}，请先处理该审批")


def _conversation_lock(conversation_id: str) -> threading.RLock:
    return _CONVERSATION_LOCKS[hash(conversation_id) % len(_CONVERSATION_LOCKS)]


def get_graph():
    global _graph
    if _graph is None:
        with _graph_lock:
            if _graph is None:
                _graph = build_graph()
    return _graph


def close_graph_runtime() -> None:
    global _graph
    _graph = None
    close_checkpointer()


def _format_result(state: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    interrupts = state.get("__interrupt__", [])
    approval_payload = interrupts[0].value if interrupts else None
    answer = state.get("final_response")
    if approval_payload:
        answer = f"该操作需要人工审批，已暂停执行并进入审批队列：{approval_payload['approval_id']}。"
    return {
        "conversation_id": conversation_id,
        "intent": state.get("intent"),
        "answer": answer,
        "tool_calls": state.get("tool_calls_proposed", []),
        "tool_results": state.get("tool_results", []),
        "resource_claims": state.get("resource_claims", []),
        "blocked_reason": state.get("error"),
        "plan": state.get("plan", []),
        "plan_source": state.get("plan_source", "unknown"),
        "retrieved_docs": state.get("retrieved_docs", []),
        "retrieved_history": state.get("retrieved_history", []),
        "summary": state.get("conversation_summary", ""),
        "memory_summary": state.get("summary", ""),
        "response_source": state.get("response_source", "unavailable"),
        "llm_status": state.get("llm_status", get_public_llm_status()),
        "fallback_reason": state.get("fallback_reason"),
        "step_budget_hit": state.get("step_budget_hit", False),
        "context": {
            **state.get("context_metrics", {}),
            "working_context": state.get("working_context", {}),
        },
        "hitl_required": bool(approval_payload) or state.get("hitl_required", False),
        "approval": approval_payload,
    }


@observe_agent
def run_copilot(
    message: str,
    roles: list[str] | None = None,
    conversation_id: str | None = None,
    user_id: str = "demo-user",
    tenant_id: str = "demo-tenant",
) -> dict[str, Any]:
    reset_llm_request_status()
    conversation_id = conversation_id or f"conversation-{uuid4()}"
    with _conversation_lock(conversation_id):
        pending = approval_store.get_pending_for_conversation(conversation_id, user_id, tenant_id)
        if pending:
            raise PendingApprovalError(pending["id"])
        stored_messages, stored_summary = memory_db.load_conversation(conversation_id, user_id, tenant_id)
        task_id = str(uuid4())
        config = {"configurable": {"thread_id": task_id}}
        state = get_graph().invoke({
            "messages": stored_messages,
            "message": message,
            "intent": "",
            "task_id": task_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "user_roles": roles or ["readonly"],
            "tenant_id": tenant_id,
            "conversation_summary": stored_summary,
            "recent_messages": [],
            "relevant_messages": [],
            "working_context": {},
            "context_metrics": {},
            "alert_payload": None,
            "resource_snapshot": None,
            "retrieved_docs": [],
            "retrieved_history": [],
            "plan": [],
            "plan_source": "pending",
            "tool_calls_proposed": [],
            "tool_results": [],
            "execution_log": [],
            "hitl_required": False,
            "hitl_approved": None,
            "final_response": "",
            "resource_claims": [],
            "response_source": "pending",
            "llm_available": True,
            "step_budget_hit": False,
            "llm_status": get_public_llm_status(),
            "fallback_reason": None,
            "summary": "",
            "error": None,
        }, config=config)
        return _format_result(state, conversation_id)


def resume_copilot(checkpoint_thread_id: str, approved: bool, approver: str, reason: str = "") -> dict[str, Any]:
    reset_llm_request_status()
    approval = approval_store.get_by_task(checkpoint_thread_id)
    if not approval:
        raise KeyError(checkpoint_thread_id)
    with _conversation_lock(approval["conversation_id"]):
        config = {"configurable": {"thread_id": checkpoint_thread_id}}
        state = get_graph().invoke(
            Command(resume={"approved": approved, "approver": approver, "reason": reason}),
            config=config,
        )
        return _format_result(state, approval["conversation_id"])
