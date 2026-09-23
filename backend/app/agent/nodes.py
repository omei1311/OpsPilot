"""LangGraph 的节点实现。

每个节点是一个 async 函数：
    输入：当前 State（和 config）
    输出：一个 dict，表示"我要更新 State 的哪几个字段"

LangGraph 会把返回的 dict 合并进 State，然后按边走向下一个节点。

节点自己通过 get_stream_writer() 往外发 SSE 事件 ——
这样"执行过程"和"状态流转"是同一件事的两面，不会出现
"状态变了但前端没收到通知"的不一致。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from app.agent.context import ToolContext
from app.agent.events import SSEEvent
from app.agent.llm import get_llm
from app.agent.policy import ALWAYS_REQUIRE_APPROVAL, assess_risk, check_batch_limit
from app.agent.prompts import (
    ANSWER_SYSTEM_PROMPT,
    INTENT_SYSTEM_PROMPT,
    build_agent_system_prompt,
)
from app.agent.state import IntentResult
from app.agent.tools import TOOL_MAP, get_tools_for_intent
from app.core.config import settings
from app.core.logging import get_logger
from app.models.agent import PendingActionStatus

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def emit(event: SSEEvent | str, data: dict[str, Any] | None = None) -> None:
    """往外发一个 SSE 事件。

    ⚠️ 必须能安全地在"非流式"场景下被调用。

    get_stream_writer() 只在 graph.astream(..., stream_mode="custom")
    或包含 custom 的模式下才有真正的 writer；用 graph.ainvoke() 直接调用时
    会抛异常。所以这里吞掉异常 —— 这样同一份节点代码既能流式跑，
    也能在单测里用 ainvoke 跑。

    事件统一包成 {"event": ..., "data": {...}}，runner 负责翻译成 SSE 帧。
    """
    try:
        writer = get_stream_writer()
        writer({"event": str(event), "data": data or {}})
    except Exception:  # noqa: BLE001
        # 非流式上下文，静默忽略即可
        pass


def _last_ai_message(messages: list) -> AIMessage | None:
    """取最近一条 AIMessage。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


# ══════════════════════════════════════════════════════════════
# 节点 1：意图识别
# ══════════════════════════════════════════════════════════════


async def understand_node(state: dict, config: RunnableConfig) -> dict[str, Any]:
    """判断用户想干什么，并决定这次运行可用哪些工具。"""
    started = time.perf_counter()
    emit(SSEEvent.THOUGHT, {"delta": "正在理解你的需求..."})

    llm = get_llm()

    try:
        # with_structured_output 让模型必须按 IntentResult 的 schema 返回。
        # 意图用 Literal 约束住了，所以下面的路由可以直接写 if/elif，
        # 不用担心模型自由发挥出一个没见过的值。
        structured = llm.with_structured_output(IntentResult)
        result: IntentResult = await structured.ainvoke(
            [
                SystemMessage(content=INTENT_SYSTEM_PROMPT),
                HumanMessage(content=state["user_input"]),
            ]
        )
        intent = result.intent
        confidence = result.confidence
        entities = result.entities
        reasoning = result.reasoning
    except Exception as exc:  # noqa: BLE001
        # ★ 意图识别失败不能让整个请求崩掉。
        # 降级成 chat，让用户至少能拿到一句解释，而不是一个 500。
        logger.warning("意图识别失败，降级为 chat | %s", exc)
        intent, confidence, entities, reasoning = (
            "chat",
            0.0,
            {},
            f"意图识别失败: {type(exc).__name__}",
        )

    # 按意图取工具白名单 —— 模型能选的工具越少，选错的概率越低
    tools = get_tools_for_intent(intent)
    tool_names = [t.name for t in tools]

    emit(
        SSEEvent.INTENT,
        {
            "intent": intent,
            "confidence": confidence,
            "entities": entities,
            "reasoning": reasoning,
            "available_tools": tool_names,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        },
    )

    logger.info(
        "意图识别 | run=%s intent=%s conf=%.2f tools=%s",
        state.get("run_id"),
        intent,
        confidence,
        tool_names,
    )

    # 把系统提示词和用户输入放进 messages，后续 agent 节点直接用
    ctx = ToolContext.from_config(config)
    system_prompt = build_agent_system_prompt(
        username=ctx.user.username,
        role=ctx.user.role,
    )

    return {
        "intent": intent,
        "intent_confidence": confidence,
        "entities": entities,
        "available_tools": tool_names,
        "messages": [
            SystemMessage(content=system_prompt),
            HumanMessage(content=state["user_input"]),
        ],
        "status": "running",
    }


# ══════════════════════════════════════════════════════════════
# 节点 2：Agent（决定下一步做什么）
# ══════════════════════════════════════════════════════════════


async def agent_node(state: dict, config: RunnableConfig) -> dict[str, Any]:
    """让模型决定：是调工具，还是直接回答。"""
    llm = get_llm()

    # 只绑定本次意图允许的工具
    tools = [TOOL_MAP[n] for n in state.get("available_tools", []) if n in TOOL_MAP]

    if not tools:
        # 没有可用工具（比如 chat 意图）就直接收尾
        return {"messages": []}

    bound = llm.bind_tools(tools)
    response: AIMessage = await bound.ainvoke(state["messages"])

    # 有思考文本就推给前端（很多模型调工具时不产生文本，这是正常的）
    if response.content:
        text = response.content if isinstance(response.content, str) else str(response.content)
        if text.strip():
            emit(SSEEvent.THOUGHT, {"delta": text})

    tool_calls = getattr(response, "tool_calls", None) or []

    # 统计 token（用于成本观察和"超预算中断"）
    usage = getattr(response, "usage_metadata", None) or {}
    tokens = int(usage.get("total_tokens", 0))

    return {
        "messages": [response],
        "step_count": state.get("step_count", 0) + 1,
        "total_tokens": state.get("total_tokens", 0) + tokens,
    }


# ══════════════════════════════════════════════════════════════
# 节点 3：执行工具
# ══════════════════════════════════════════════════════════════


async def tools_node(state: dict, config: RunnableConfig) -> dict[str, Any]:
    """执行模型请求的工具调用。"""
    ai_msg = _last_ai_message(state["messages"])
    if ai_msg is None:
        return {}

    tool_calls = getattr(ai_msg, "tool_calls", None) or []
    tool_messages: list[ToolMessage] = []
    observations: list[dict[str, Any]] = list(state.get("observations", []))
    affected: list[int] = list(state.get("affected_ticket_ids", []))

    for call in tool_calls:
        name = call["name"]
        args = call.get("args") or {}
        call_id = call.get("id") or f"call_{uuid.uuid4().hex[:8]}"

        emit(
            SSEEvent.TOOL_CALL,
            {"call_id": call_id, "tool": name, "args": args},
        )

        started = time.perf_counter()
        tool = TOOL_MAP.get(name)

        if tool is None:
            result = {"ok": False, "error": f"工具 {name} 不存在"}
        else:
            try:
                # ★ 身份通过 config 传给工具，而不是作为参数。
                # 见 app/agent/context.py 里的详细说明。
                result = await tool.ainvoke(args, config=config)
            except Exception as exc:  # noqa: BLE001
                # 工具抛异常时不能让图崩溃 —— 把错误变成结构化结果回灌给模型，
                # 模型还有机会换个方式重试或者告诉用户。
                logger.exception("工具执行异常 | %s", name)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        duration_ms = int((time.perf_counter() - started) * 1000)
        ok = bool(result.get("ok"))
        summary = _summarize_result(name, result)

        emit(
            SSEEvent.TOOL_RESULT,
            {
                "call_id": call_id,
                "tool": name,
                "ok": ok,
                "summary": summary,
                "duration_ms": duration_ms,
                "data_preview": _preview(result),
            },
        )

        # 记录观测结果。这份数据不进 LLM 上下文（messages 才是），
        # 主要给前端展示、生成确认卡片预览、以及收集引用来源。
        #
        # ⚠️ 这里【必须】存一份完整的结构化数据，不能事后去解析 ToolMessage。
        #
        # 原因：为了控制 token，回灌给模型的 JSON 会被截断到 3000 字符
        # （见 _truncate）。截断是从中间切断的，JSON 结构已经被破坏，
        # 再去 json.loads 必然失败 —— 而且失败得很安静（走 except 分支），
        # 表现为"确认卡片里一条预览都没有"，很难查。
        #
        # 教训：需要后续使用的结构化数据，要在产生它的那一刻就存下来，
        # 不要指望从"给人/模型看的展示文本"里再解析回来。
        observation: dict[str, Any] = {
            "tool": name,
            "args": args,
            "ok": ok,
            "summary": summary,
            "row_count": result.get("count") or result.get("affected_count"),
        }

        # 列表类结果保留精简版，供确认卡片预览和引用来源使用
        if isinstance(result.get("items"), list):
            observation["preview_items"] = [
                {
                    # ★ id 必须带上。
                    # 前端确认卡片要靠它实现"逐条取消勾选"——
                    # 用户取消勾选后，前端把剩下的 id 列表回传，
                    # 后端按新列表执行。没有 id 这个交互就做不了。
                    "id": it.get("id"),
                    "ticket_no": it.get("ticket_no"),
                    "title": it.get("title"),
                    "priority": it.get("priority"),
                    "status": it.get("status"),
                    "sla_status": it.get("sla_status"),
                }
                for it in result["items"][:100]
                if isinstance(it, dict)
            ]
            observation["total"] = result.get("total")
            observation["truncated"] = result.get("truncated")

        # 部门列表也存一份，用于把 department_id 翻译成中文名称
        if isinstance(result.get("departments"), list):
            observation["departments"] = [
                {"id": d.get("id"), "name": d.get("name")}
                for d in result["departments"]
                if isinstance(d, dict)
            ]

        # ★ 记录这次的检索是否被截断了。
        # 场景 4 里这个信号极其重要：用户说"所有超过 24 小时的工单"，
        # 如果 limit 只取了 20 条而实际有 22 条，就会漏掉 2 条 ——
        # 而且没有任何报错，用户完全看不出来。这是最危险的一类 bug。
        observations.append(observation)

        # 收集这次运行影响到的工单 ID（用于最终回答里的引用和审计）
        for key in ("id",):
            if isinstance(result.get(key), int):
                affected.append(result[key])

        # ★ 关键：把工具结果作为 ToolMessage 回灌给模型。
        # 内容要序列化成字符串 —— 模型只能读文本。
        # 但要【截断】，否则一个返回 50 条工单的工具会把上下文撑爆。
        tool_messages.append(
            ToolMessage(
                content=_truncate(json.dumps(result, ensure_ascii=False, default=str)),
                tool_call_id=call_id,
                name=name,
            )
        )

    return {
        "messages": tool_messages,
        "observations": observations,
        "affected_ticket_ids": affected,
    }


def _summarize_result(name: str, result: dict[str, Any]) -> str:
    """把工具结果压成一句人话，给前端展示用。"""
    if not result.get("ok"):
        return f"失败：{result.get('error', '未知错误')}"

    if "total" in result:
        return f"找到 {result['total']} 条"
    if "count" in result:
        return f"返回 {result['count']} 条"
    if "ticket_no" in result:
        return f"工单 {result['ticket_no']}"
    if "departments" in result:
        return f"{len(result['departments'])} 个部门"
    if result.get("pending_approval"):
        return f"待确认，影响 {result.get('affected_count', 0)} 条"
    return "完成"


def _preview(result: dict[str, Any], limit: int = 300) -> Any:
    """给前端的预览数据，控制大小。"""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return result
    return {"_truncated": True, "_preview": text[:limit] + "..."}


def _truncate(text: str, limit: int = 12000) -> str:
    """截断回灌给模型的内容。

    ⚠️ 这个截断是必要的，不是偷懒。
    list_tickets 一次最多返回 100 条，JSON 可能有 20KB ≈ 5000 token。
    几轮下来上下文会爆，既贵又会让模型注意力涣散。

    ⚠️ 但阈值也不能太小。之前设的 3000 字符装不下 20 条工单，
    结果是模型看到的数据被腰斩 —— 更糟的是我当时还试图
    解析这段被截断的 JSON 来生成确认卡片预览，必然失败。

    现在设 12000，约能装下 100 条精简后的工单摘要。
    截断后仍然明确告知（模型需要知道自己没看到全部数据）。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[内容过长，已截断，原始长度 {len(text)} 字符]"


# ══════════════════════════════════════════════════════════════
# 节点 4：生成执行计划（HITL）
# ══════════════════════════════════════════════════════════════


async def guard_node(state: dict, config: RunnableConfig) -> dict[str, Any]:
    """把高风险工具调用转成一份待确认的计划，写库，然后结束图。

    ★ 这里【不执行】任何写操作，只生成计划。

    为什么不是用 LangGraph 的 interrupt() 挂起？
    见 app/agent/runner.py 顶部的说明 —— 我们用"两阶段"方案，
    计划落在 MySQL 里，用户确认后由另一个入口执行。
    """
    ctx = ToolContext.from_config(config)
    ai_msg = _last_ai_message(state["messages"])
    tool_calls = getattr(ai_msg, "tool_calls", None) or []

    if not tool_calls:
        return {"status": "running"}

    # 取第一个高风险调用（正常情况下批量意图只会产生一个）
    target = tool_calls[0]
    name = target["name"]
    args = target.get("args") or {}
    ticket_ids = args.get("ticket_ids") or []

    # ── 安全阀：数量上限 ───────────────────────────────────
    ok, msg = check_batch_limit(len(ticket_ids))
    if not ok:
        emit(SSEEvent.ERROR, {"code": "BATCH_LIMIT_EXCEEDED", "message": msg})
        return {
            "status": "failed",
            "error": msg,
            "answer": msg,
            "messages": [
                ToolMessage(
                    content=json.dumps({"ok": False, "error": msg}, ensure_ascii=False),
                    tool_call_id=target.get("id") or "guard",
                    name=name,
                )
            ],
        }

    # ── 组装计划 ───────────────────────────────────────────
    plan = _build_plan(name, args, state)

    # ── 落库 ───────────────────────────────────────────────
    from app.models.agent import AgentPendingAction

    action_id = str(uuid.uuid4())
    expires_at = _now() + timedelta(seconds=settings.AGENT_APPROVAL_TIMEOUT)

    action = AgentPendingAction(
        action_id=action_id,
        run_id=state["run_id"],
        conversation_id=state["conversation_id"],
        requested_by=ctx.user.id,
        title=plan["title"],
        risk_level="high",
        payload={"tool": name, "args": args, "steps": plan["steps"]},
        affected_count=len(ticket_ids),
        preview=plan["preview"],
        status=PendingActionStatus.PENDING,
        expires_at=expires_at,
    )
    ctx.db.add(action)
    await ctx.db.commit()

    # ── 推事件 ─────────────────────────────────────────────
    #
    # ★ 这里要补发一个 tool_call 事件。
    #
    # 正常流程里 tool_call 是 tools_node 发的，但高风险调用被
    # route_after_agent 直接路由到了 guard，绕过了 tools_node。
    # 不补发的话前端时间线里会缺一块 —— 用户看到"查了工单，然后突然
    # 冒出个确认框"，中间少了"准备执行批量修改"这一步。
    emit(
        SSEEvent.TOOL_CALL,
        {
            "call_id": target.get("id") or "guard",
            "tool": name,
            "args": args,
            "risk_level": "high",
            "requires_approval": True,
        },
    )

    emit(
        SSEEvent.PLAN,
        {
            "action_id": action_id,
            "title": plan["title"],
            "steps": plan["steps"],
            "affected_count": len(ticket_ids),
            "risk_level": "high",
            "preview": plan["preview"],
            "expires_at": expires_at.isoformat() + "Z",
            "timeout_seconds": settings.AGENT_APPROVAL_TIMEOUT,
        },
    )
    emit(
        SSEEvent.AWAITING_APPROVAL,
        {"action_id": action_id, "timeout_seconds": settings.AGENT_APPROVAL_TIMEOUT},
    )

    logger.info(
        "生成待确认计划 | action_id=%s tool=%s affected=%s run=%s",
        action_id,
        name,
        len(ticket_ids),
        state["run_id"],
    )

    # 给模型一个 ToolMessage，让它知道"已经提交确认，等待用户操作"。
    # 这样如果图继续往下走，模型不会以为工具失败了。
    # 实际上 guard 之后图就结束了。
    return {
        "plan": plan,
        "pending_action_id": action_id,
        "status": "awaiting_approval",
    }


def _build_plan(name: str, args: dict[str, Any], state: dict) -> dict[str, Any]:
    """把工具调用翻译成给用户看的计划。"""
    ticket_ids = args.get("ticket_ids") or []
    changes = {
        k: v
        for k, v in args.items()
        if k != "ticket_ids" and v is not None
    }

    # 把技术字段翻译成中文
    LABELS = {
        "priority": "优先级",
        "category": "分类",
        "department_id": "负责部门",
        "assignee_id": "负责人",
    }
    VALUE_LABELS = {
        "low": "低", "medium": "中", "high": "高", "urgent": "紧急",
        "account": "账号", "payment": "支付", "technical": "技术",
        "logistics": "物流", "operation": "运营", "other": "其他",
    }

    # ★ 把 department_id 换成部门名称。
    #
    # 不换的话确认卡片上会显示"负责部门改为「1」"——
    # 用户看到的是一个数字，根本没法核对自己选的是不是"技术部"。
    # 确认卡片存在的意义就是让用户能核对，显示 ID 等于白做。
    #
    # 部门名称从之前的 list_departments 工具结果里取，不额外查库。
    dept_names = _extract_department_names(state)
    if "department_id" in changes:
        dept_id = changes["department_id"]
        changes["department_id"] = dept_names.get(dept_id, f"部门#{dept_id}")

    change_desc = "、".join(
        f"{LABELS.get(k, k)}改为「{VALUE_LABELS.get(str(v), v)}」"
        for k, v in changes.items()
    )

    if name == "batch_assign_tickets":
        title = f"将 {len(ticket_ids)} 条工单分派（{change_desc}）"
        step_desc = f"批量分派到 {change_desc}"
    else:
        title = f"将 {len(ticket_ids)} 条工单{change_desc}"
        step_desc = change_desc

    # 预览数据：从候选工单里取摘要，让用户在确认前能核对
    preview = _extract_preview(state)

    # ⚠️ affected_count 必须以【实际要改的 ID 数量】为准，而不是预览的数量。
    # preview 最多只取 50 条用于展示，但它不是执行范围。
    # 之前漏了这个字段，导致生成回答时读到默认值 0，
    # 用户看到"我找到了 0 条符合条件的工单"—— 数字自相矛盾。
    return {
        "title": title,
        "affected_count": len(ticket_ids),
        "steps": [
            {
                "seq": 1,
                "tool": name,
                "args": args,
                "description": step_desc,
                "affected_count": len(ticket_ids),
            }
        ],
        "preview": preview,
    }


def _extract_department_names(state: dict) -> dict[int, str]:
    """从观测结果里取出 department_id → 部门名称的映射。

    和 _extract_preview 一样的思路：数据在工具执行时就存进 observations，
    事后不去解析 ToolMessage。保持一致，少一类 bug。
    """
    names: dict[int, str] = {}
    for obs in state.get("observations", []):
        for d in obs.get("departments") or []:
            if isinstance(d.get("id"), int):
                names[d["id"]] = d.get("name") or f"部门#{d['id']}"
    return names


def _extract_preview(state: dict) -> list[dict[str, Any]]:
    """从工具观测结果里捞出这批工单的摘要。

    用户确认时需要看到"具体是哪 17 条"，而不是"17 条"这个数字 ——
    数字没法核对，看到具体列表才能发现"哦这条不该改"。

    数据来源是 observations（工具执行时存好的结构化数据），
    不是 ToolMessage —— 后者为了省 token 被截断过，JSON 已经不完整了。
    """
    for obs in reversed(state.get("observations", [])):
        if obs.get("tool") == "list_tickets" and obs.get("preview_items"):
            return obs["preview_items"][:50]
    return []


# ══════════════════════════════════════════════════════════════
# 节点 5：生成最终回答
# ══════════════════════════════════════════════════════════════


async def respond_node(state: dict, config: RunnableConfig) -> dict[str, Any]:
    """生成给用户看的最终回答。"""
    llm = get_llm()
    emit(SSEEvent.THOUGHT, {"delta": "正在整理结果..."})

    # 特殊情况：guard 已经写好了回答（比如超限被拦），直接用
    if state.get("answer"):
        answer = state["answer"]
        emit(SSEEvent.TOKEN, {"delta": answer})
        return {"answer": answer}

    intent = state.get("intent", "chat")

    # chat 意图不需要工具结果，直接回答
    if intent == "chat":
        try:
            resp = await llm.ainvoke(
                [
                    SystemMessage(
                        content=(
                            "你是 OpsPilot 的智能工单助手。"
                            "用户的话与工单业务无关，或者只是想打个招呼。"
                            "请友好、简短地回应，并说明你可以帮他做什么："
                            "创建工单、查询工单、分析 SLA 风险、批量处理工单。"
                            "回答控制在 100 字以内。"
                        )
                    ),
                    HumanMessage(content=state["user_input"]),
                ]
            )
            answer = _to_text(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("闲聊回答生成失败 | %s", exc)
            answer = "你好，我是 OpsPilot 智能工单助手。可以帮你创建工单、查询工单、分析 SLA 风险。"
    else:
        try:
            resp = await llm.ainvoke(
                [*state["messages"], SystemMessage(content=ANSWER_SYSTEM_PROMPT)]
            )
            answer = _to_text(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.exception("最终回答生成失败")
            answer = f"抱歉，生成回答时出错了：{type(exc).__name__}"

    emit(SSEEvent.TOKEN, {"delta": answer})

    # 引用来源：把这次真正查到的工单列出来，前端可点击跳转。
    # ★ 这是防幻觉的一个手段 —— 回答里提到的工单必须来自真实查询结果。
    citations = _collect_citations(state)
    if citations:
        emit(SSEEvent.CITATIONS, {"items": citations})

    return {"answer": answer, "citations": citations, "status": "success"}


def _to_text(content: Any) -> str:
    """把模型返回的 content 统一转成字符串。

    有些模型（多模态的）会返回 list[dict] 而不是 str，
    直接 str() 会得到一串难看的 repr。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _collect_citations(state: dict) -> list[dict[str, Any]]:
    """收集这次运行真正查到的工单，作为回答的引用来源。

    同样从 observations 取，不解析 ToolMessage（原因见 _extract_preview）。

    这个列表的作用是【防幻觉】：回答里提到的工单必须来自真实查询结果，
    前端把它渲染成可点击的链接，用户一眼就能核对 AI 有没有编造。
    """
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()

    for obs in state.get("observations", []):
        items = obs.get("preview_items") or []
        for item in items:
            no = item.get("ticket_no")
            if no and no not in seen:
                seen.add(no)
                citations.append({"ticket_no": no, "title": item.get("title") or ""})

    return citations[:20]
