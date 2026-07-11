from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph

from backend.agent.llm import call_deepseek, summarize_messages
from backend.guardrails.policy import detect_write_intent, validate_tool_calls
from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import APPROVAL_QUEUE, call_tool
from backend.memory.context_manager import deterministic_summary, manage_context_window
from backend.memory.database import memory_db
from backend.memory.retriever import retrieve, retrieve_history, retrieve_skill_detail
from backend.memory.store import write_conversation_summary
from backend.skills.loader import startup_skill_summaries


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
    alert_payload: dict | None
    resource_snapshot: dict | None
    retrieved_docs: list[dict]
    retrieved_history: list[dict]
    plan: list[str]
    tool_calls_proposed: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    hitl_required: bool
    hitl_approved: bool | None
    execution_log: list[dict[str, Any]]
    final_response: str
    summary: str
    error: str | None


SYSTEM_PROMPT = f"""你是 DCS/FusionCompute 运维 Copilot。
回答要自然，但在识别到运维意图时必须基于工具结果和检索到的 Skill。
不要编造工具结果之外的资源状态。写操作、变更、重启、删除、扩容只能进入审批，不能声称已执行。
输出优先包含：结论、证据、建议动作、风险/下一步。

可用 Skill 第一层：
{startup_skill_summaries()}"""


def _contains(message: str, words: list[str]) -> bool:
    return any(word in message for word in words)


def classify_intent(message: str) -> str:
    stripped = message.strip()
    if stripped.lower() in {"hi", "hello"} or stripped in {"你好", "您好", "嗨", "在吗", "谢谢", "感谢", "辛苦了"}:
        return "smalltalk"
    if _contains(message, ["第一条", "第二条", "第三条", "上一条", "下一条", "这条", "那条", "它呢"]):
        return "alert_explain"
    if _contains(message, ["告警", "报警", "alarm"]):
        return "alert_explain"
    if _contains(message, ["容量", "撑多久", "预测", "剩余", "扩容"]):
        return "capacity_forecast"
    if _contains(message, ["变慢", "性能", "卡", "CPU", "cpu", "内存", "延迟", "dcs-app"]):
        return "vm_diagnosis"
    if _contains(message, ["虚拟机", "云服务器", "VM", "vm", "主机", "集群", "数据存储", "存储", "资源", "资产", "环境", "概览", "总览", "平台情况", "有哪些", "有多少", "列表"]):
        return "resource_query"
    if detect_write_intent(message):
        return "change_execute"
    return "general"


def extract_cluster_id(message: str) -> str:
    match = re.search(r"cluster-\d+", message, re.I)
    return match.group(0).lower() if match else "cluster-002"


def extract_vm_id(message: str) -> str:
    match = re.search(r"(vm-\d+|dcs-[a-z0-9-]+)", message, re.I)
    return match.group(0) if match else "dcs-app-01"


def extract_alarm_id(message: str, history: list[dict[str, str]] | None = None) -> str | None:
    explicit = re.search(r"alarm-\d+", message, re.I)
    if explicit:
        return explicit.group(0).lower()
    ordinal_map = {"第一条": 0, "第1条": 0, "第二条": 1, "第2条": 1, "第三条": 2, "第3条": 2}
    for keyword, index in ordinal_map.items():
        if keyword in message:
            alarms = call_tool(ToolRequest(tool_name="list_alarms", params={})).data or []
            if index < len(alarms):
                return alarms[index]["id"]
    if _contains(message, ["它呢", "这条", "那条", "这个", "那个"]):
        for item in reversed(history or []):
            matches = re.findall(r"alarm-\d+", item.get("content", ""), re.I)
            if matches:
                return matches[-1].lower()
    return None


def make_plan(intent: str, message: str) -> list[str]:
    plans = {
        "alert_explain": ["查询告警列表", "必要时查询告警详情", "结合 Skill 给出处置建议"],
        "capacity_forecast": ["查询集群容量", "运行容量预测", "输出风险等级和建议"],
        "vm_diagnosis": ["查询 VM 详情", "查询 VM 性能指标", "关联告警并给出诊断"],
        "resource_query": ["查询资源总览", "按用户问题筛选资源类型", "输出数量或列表"],
        "change_execute": ["识别高风险变更", "进入护栏和审批流程", "不直接执行"],
        "smalltalk": ["自然回应"],
    }
    return plans.get(intent, ["自然回应或引导用户说明运维目标"])


def plan_tools(intent: str, message: str, history: list[dict[str, str]]) -> list[dict[str, Any]]:
    if intent == "alert_explain":
        calls = [{"tool_name": "list_alarms", "params": {}}]
        alarm_id = extract_alarm_id(message, history)
        asks_list = _contains(message, ["哪些", "列表", "多少", "当前", "现在", "所有"])
        if alarm_id:
            calls += [
                {"tool_name": "get_alarm_detail", "params": {"alarm_id": alarm_id}},
                {"tool_name": "get_cluster_capacity", "params": {"cluster_id": "cluster-002"}},
            ]
        elif not asks_list:
            calls += [
                {"tool_name": "get_alarm_detail", "params": {"alarm_id": "alarm-9001"}},
                {"tool_name": "get_cluster_capacity", "params": {"cluster_id": "cluster-002"}},
            ]
        return calls
    if intent == "capacity_forecast":
        cluster_id = extract_cluster_id(message)
        return [
            {"tool_name": "get_cluster_capacity", "params": {"cluster_id": cluster_id}},
            {"tool_name": "run_capacity_forecast", "params": {"cluster_id": cluster_id, "forecast_days": 30}},
        ]
    if intent == "vm_diagnosis":
        vm_id = extract_vm_id(message)
        return [
            {"tool_name": "get_vm_detail", "params": {"vm_id": vm_id}},
            {"tool_name": "get_vm_metrics", "params": {"vm_id": vm_id, "time_range": "1h"}},
            {"tool_name": "list_alarms", "params": {}},
        ]
    if intent == "resource_query":
        return [
            {"tool_name": "get_resource_overview", "params": {}},
            {"tool_name": "list_clusters", "params": {}},
            {"tool_name": "list_vms", "params": {}},
        ]
    if intent == "change_execute":
        return [{"tool_name": "create_approval_request", "params": {"title": "高风险变更请求", "description": message, "risk": "high"}}]
    return []


def intent_classifier(state: CopilotState) -> CopilotState:
    return {"intent": classify_intent(state["message"])}


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
    )
    snapshot = call_tool(ToolRequest(tool_name="get_resource_overview", params={}, caller_roles=state["user_roles"]))
    return {
        "recent_messages": context["recent_messages"],
        "conversation_summary": context["conversation_summary"],
        "resource_snapshot": snapshot.data if snapshot.success else None,
        "alert_payload": None,
    }


def memory_retriever(state: CopilotState) -> CopilotState:
    docs = retrieve(state["message"], state["user_roles"], top_k=3, tenant_id=state["tenant_id"], tier=2)
    history = retrieve_history(state["message"], state["user_roles"], top_k=2, tenant_id=state["tenant_id"])
    return {"retrieved_docs": docs, "retrieved_history": history}


def planner(state: CopilotState) -> CopilotState:
    calls = plan_tools(state["intent"], state["message"], state.get("recent_messages", []))
    docs = list(state.get("retrieved_docs", []))
    if docs:
        detail = retrieve_skill_detail(docs[0]["id"], state["user_roles"], state["tenant_id"])
        if detail:
            docs.append(detail)
    return {
        "plan": make_plan(state["intent"], state["message"]),
        "tool_calls_proposed": calls,
        "retrieved_docs": docs,
    }


def guardrail(state: CopilotState) -> CopilotState:
    result = validate_tool_calls(state.get("tool_calls_proposed", []), state["user_roles"], state["message"])
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
    item = {
        "id": f"approval-{len(APPROVAL_QUEUE) + 1:04d}",
        "title": "待审批变更",
        "description": state["message"],
        "risk": "high",
        "status": "pending",
    }
    APPROVAL_QUEUE.append(item)
    return {"hitl_approved": False, "final_response": f"该操作需要人工审批，已进入审批队列：{item['id']}。"}


def tool_executor(state: CopilotState) -> CopilotState:
    results = []
    execution_log = []
    for call in state.get("tool_calls_proposed", []):
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


def _tool_data(state: CopilotState) -> dict[str, Any]:
    return {item["tool_name"]: item.get("data") for item in state.get("tool_results", []) if item.get("success")}


def deterministic_response(state: CopilotState) -> str:
    intent = state["intent"]
    message = state["message"]
    if state.get("error"):
        return f"请求被护栏拦截：{state['error']}"
    if state.get("final_response"):
        return state["final_response"]
    if intent == "smalltalk":
        return "你好，我在。你可以自然地问我资源、告警、容量、VM 性能，也可以继续追问上一轮结果。"
    data = _tool_data(state)
    if intent == "alert_explain":
        alarms = data.get("list_alarms") or []
        asks_list = _contains(message, ["哪些", "列表", "多少", "当前", "现在", "所有"]) and not extract_alarm_id(message, state.get("recent_messages", []))
        if asks_list:
            alarm_lines = "\n".join(f"- {a['id']}：{a['name']}，级别 {a['severity']}，对象 {a['object_type']} / {a['object_id']}，状态 {a['status']}" for a in alarms)
            return f"当前共有 {len(alarms)} 条活动告警。\n\n{alarm_lines}\n\n你可以继续问：第一条告警的原因？第二条呢？"
        detail = data.get("get_alarm_detail") or {}
        alarm = detail.get("alarm", {})
        related = detail.get("related", {})
        if alarm.get("object_type") == "host":
            host = related.get("host") or {}
            return f"结论：{alarm.get('id')} 的主要风险是 {alarm.get('name')}。\n\n证据：关联主机 {host.get('name')} 状态 {host.get('status')}，CPU {int(host.get('cpu_usage', 0) * 100)}%，内存 {int(host.get('memory_usage', 0) * 100)}%。\n\n建议动作：检查同主机 VM 和近期任务峰值，必要时迁移部分 VM 或规划扩容。\n\n风险提示：不要直接重启主机，先确认业务窗口和 HA 策略。"
        ds = related.get("datastore") or {}
        return f"结论：{alarm.get('id')} 的主要风险是 {alarm.get('name')}。\n\n证据：关联数据存储 {ds.get('name')} 剩余 {ds.get('free_gb')}GB / 总量 {ds.get('capacity_gb')}GB。\n\n建议动作：先清理过期快照和低价值镜像，确认增长最快的 VM；如无法释放空间，准备扩容或迁移计划。\n\n风险提示：不要直接删除未知磁盘或快照，先确认业务归属和备份状态。"
    if intent == "capacity_forecast":
        forecast = data.get("run_capacity_forecast") or {}
        return f"结论：{forecast.get('cluster_id')} 容量风险为 {forecast.get('risk_level')}。\n\n证据：日增长约 {forecast.get('daily_growth_gb')}GB，预计 {forecast.get('days_to_exhaustion')} 天后耗尽。\n\n建议动作：{forecast.get('recommendation')}"
    if intent == "vm_diagnosis":
        metrics = (data.get("get_vm_metrics") or {}).get("series", [])
        warnings = [m for m in metrics if m.get("status") == "warning"]
        warning_text = "；".join(f"{m['metric']}={m['value']}{m['unit']}" for m in warnings) or "未发现明显异常"
        return f"结论：该 VM 的性能问题优先排查 CPU ready、CPU usage 和存储延迟。\n\n证据：{warning_text}。\n\n建议动作：先确认所在主机是否过载，再检查同主机 VM 的 CPU 争用；若磁盘延迟持续高于 20ms，继续排查数据存储。"
    if intent == "resource_query":
        resource = data.get("get_resource_overview") or {}
        overview = resource.get("overview", {})
        vms = resource.get("vms", [])
        hosts = resource.get("hosts", [])
        clusters = resource.get("clusters", [])
        datastores = resource.get("datastores", [])
        if _contains(message, ["虚拟机", "云服务器", "VM", "vm"]):
            lines = "\n".join(f"- {vm['name']}：{vm['status']}，{vm['cpu']} vCPU，{vm['memory_mb']}MB，IP {vm['ip']}，所在主机 {vm['host_id']}" for vm in vms)
            return f"当前共有 {len(vms)} 台虚拟机。\n\n{lines}"
        if "主机" in message:
            lines = "\n".join(f"- {h['name']}：{h['status']}，管理 IP {h['management_ip']}，CPU {int(h['cpu_usage'] * 100)}%，内存 {int(h['memory_usage'] * 100)}%" for h in hosts)
            return f"当前共有 {len(hosts)} 台主机。\n\n{lines}"
        if "集群" in message:
            lines = "\n".join(f"- {c['name']}：{c['status']}，主机 {c['host_count']} 台，VM {c['vm_count']} 台" for c in clusters)
            return f"当前共有 {len(clusters)} 个集群。\n\n{lines}"
        if _contains(message, ["存储", "数据存储"]):
            lines = "\n".join(f"- {d['name']}：{d['status']}，剩余 {d['free_gb']}GB / 总量 {d['capacity_gb']}GB" for d in datastores)
            return f"当前共有 {len(datastores)} 个数据存储。\n\n{lines}"
        return f"资源盘点：站点 {len(resource.get('sites', []))} 个，集群 {overview.get('cluster_count')} 个，主机 {overview.get('host_count')} 台，VM {overview.get('vm_count')} 台，数据存储 {overview.get('datastore_count')} 个，活跃告警 {overview.get('active_alarm_count')} 条。"
    if intent == "change_execute":
        return "该请求涉及写操作，已按护栏要求进入审批流程，demo 环境不会直接执行变更。"
    return "我在。你可以问资源、告警、容量预测、VM 性能诊断，也可以继续追问上一轮结果。"


def response_generator(state: CopilotState) -> CopilotState:
    prompt = json.dumps({
        "message": state["message"],
        "intent": state["intent"],
        "conversation_summary": state.get("conversation_summary", ""),
        "recent_messages": state.get("recent_messages", []),
        "retrieved_docs": state.get("retrieved_docs", []),
        "plan": state.get("plan", []),
        "tool_results": state.get("tool_results", []),
        "error": state.get("error"),
    }, ensure_ascii=False, indent=2)
    llm_response = None
    try:
        llm_response = call_deepseek(SYSTEM_PROMPT, prompt)
    except Exception as exc:
        state.setdefault("tool_results", []).append({"tool_name": "deepseek", "success": False, "error_msg": str(exc)})
    return {"final_response": llm_response or deterministic_response(state)}


def memory_writer(state: CopilotState) -> CopilotState:
    turn_summary = f"intent={state.get('intent')}; message={state.get('message')[:120]}; tools={[r.get('tool_name') for r in state.get('tool_results', [])]}"
    write_conversation_summary(state["task_id"], turn_summary, state.get("execution_log", []))
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


def route_after_guardrail(state: CopilotState) -> Literal["hitl_interrupt", "tool_executor", "response_generator"]:
    if state.get("error"):
        return "response_generator"
    if state.get("hitl_required"):
        return "hitl_interrupt"
    if state.get("tool_calls_proposed"):
        return "tool_executor"
    return "response_generator"


def build_graph():
    builder = StateGraph(CopilotState)
    for name, fn in [
        ("intent_classifier", intent_classifier),
        ("context_loader", context_loader),
        ("memory_retriever", memory_retriever),
        ("planner", planner),
        ("guardrail", guardrail),
        ("hitl_interrupt", hitl_interrupt),
        ("tool_executor", tool_executor),
        ("response_generator", response_generator),
        ("memory_writer", memory_writer),
        ("error_handler", error_handler),
    ]:
        builder.add_node(name, fn)
    builder.set_entry_point("intent_classifier")
    builder.add_edge("intent_classifier", "context_loader")
    builder.add_edge("context_loader", "memory_retriever")
    builder.add_edge("memory_retriever", "planner")
    builder.add_edge("planner", "guardrail")
    builder.add_conditional_edges("guardrail", route_after_guardrail)
    builder.add_edge("hitl_interrupt", "response_generator")
    builder.add_edge("tool_executor", "response_generator")
    builder.add_edge("response_generator", "memory_writer")
    builder.add_edge("memory_writer", END)
    return builder.compile()


GRAPH = build_graph()


def run_copilot(
    message: str,
    roles: list[str] | None = None,
    history: list[dict[str, str]] | None = None,
    summary: str = "",
    conversation_id: str | None = None,
    user_id: str = "demo-user",
    tenant_id: str = "demo-tenant",
) -> dict[str, Any]:
    conversation_id = conversation_id or f"conversation-{uuid4()}"
    messages = history or []
    if not messages:
        stored_messages, stored_summary = memory_db.load_conversation(conversation_id)
        messages = stored_messages
        summary = summary or stored_summary
    state = GRAPH.invoke({
        "messages": messages,
        "message": message,
        "task_id": "demo-task",
        "conversation_id": conversation_id,
        "user_id": user_id,
        "user_roles": roles or ["readonly"],
        "tenant_id": tenant_id,
        "conversation_summary": summary,
        "tool_results": [],
        "execution_log": [],
        "hitl_required": False,
        "hitl_approved": None,
        "error": None,
    })
    return {
        "intent": state.get("intent"),
        "answer": state.get("final_response"),
        "tool_calls": state.get("tool_calls_proposed", []),
        "tool_results": state.get("tool_results", []),
        "blocked_reason": state.get("error"),
        "plan": state.get("plan", []),
        "retrieved_docs": state.get("retrieved_docs", []),
        "summary": state.get("conversation_summary", ""),
        "memory_summary": state.get("summary", ""),
        "hitl_required": state.get("hitl_required", False),
    }
