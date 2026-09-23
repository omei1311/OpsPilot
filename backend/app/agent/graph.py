"""LangGraph 状态图。

图结构::

    START
      │
      ▼
   understand ──(chat)──────────────────┐
      │                                 │
    (其他意图)                           │
      ▼                                 │
    agent ◄──────┐                      │
      │          │                      │
   ┌──┴───┐      │                      │
   │      │      │                      │
 (要调工具) (直接答) │                    │
   │      │      │                      │
   │      └──────┼──────────────────────┤
   │             │                      │
   ├─(高风险批量)─→ guard ──→ END         │
   │             │                      │
   └─(普通工具)──→ tools ────────────────┤
                  │                      │
                  └──────────────────────┤
                                         ▼
                                      respond ──→ END

★ 关于 HITL 的范围

本项目的 HITL 只拦截【批量操作】（batch_update_tickets / batch_assign_tickets）。
这是场景 4 的核心需求，也是风险最集中的地方。

单条操作（改一张工单的优先级、关一张工单）不弹确认框，原因是：
  · 用户明确指定了具体工单，意图清晰
  · 影响面小，且可以通过再次操作纠正
  · 什么都弹确认会产生"确认疲劳" —— 用户条件反射点同意，
    反而让真正需要关注的批量操作失去了警惕性

policy.py 里仍然对单条操作做了风险分级（升级优先级=high），
那是留给后续扩展的口子：如果哪天业务要求单条也要确认，
只需在 route_after_agent 里把返回值改成 "guard" 即可，
guard_node 本身就支持处理单条调用。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    agent_node,
    guard_node,
    respond_node,
    tools_node,
    understand_node,
)
from app.agent.policy import ALWAYS_REQUIRE_APPROVAL
from app.agent.state import AgentState
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 条件路由
# ══════════════════════════════════════════════════════════════


def route_after_understand(state: AgentState) -> str:
    """意图识别之后：闲聊直接回答，其他交给 agent。"""
    if state.get("intent") == "chat":
        return "respond"
    # 没有可用工具（理论上不会发生）也直接回答，避免空转
    if not state.get("available_tools"):
        return "respond"
    return "agent"


def route_after_agent(state: AgentState) -> str:
    """agent 节点产出之后：

        没有工具调用     → respond（模型认为可以直接回答了）
        触发了循环保护   → respond（强制收尾）
        要调高风险批量工具 → guard（生成计划等确认）
        其他             → tools（正常执行）
    """
    messages = state.get("messages", [])
    if not messages:
        return "respond"

    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return "respond"

    # ── 循环保护 ───────────────────────────────────────────
    #
    # 必须有两道：步数限制和 token 限制。
    # 只限步数不够 —— 一步可能处理很大的上下文；
    # 只限 token 也不够 —— 空转的步骤消耗很少但会一直转下去。
    if state.get("step_count", 0) >= settings.AGENT_MAX_STEPS:
        logger.warning(
            "触发最大步数限制 | run=%s steps=%s",
            state.get("run_id"),
            state.get("step_count"),
        )
        return "respond"

    if state.get("total_tokens", 0) >= settings.AGENT_MAX_TOKENS_PER_RUN:
        logger.warning(
            "触发 token 上限 | run=%s tokens=%s",
            state.get("run_id"),
            state.get("total_tokens"),
        )
        return "respond"

    # ── 高风险拦截 ─────────────────────────────────────────
    for call in tool_calls:
        if call.get("name") in ALWAYS_REQUIRE_APPROVAL:
            logger.info(
                "检测到高风险批量操作，转入确认流程 | run=%s tool=%s",
                state.get("run_id"),
                call.get("name"),
            )
            return "guard"

    return "tools"


# ══════════════════════════════════════════════════════════════
# 建图
# ══════════════════════════════════════════════════════════════


def build_graph() -> StateGraph:
    """构造状态图（未编译）。

    单独抽出来是为了让测试能拿到未编译的图做结构断言。
    """
    builder = StateGraph(AgentState)

    # ── 节点 ───────────────────────────────────────────────
    builder.add_node("understand", understand_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("guard", guard_node)
    builder.add_node("respond", respond_node)

    # ── 边 ─────────────────────────────────────────────────
    builder.add_edge(START, "understand")

    # understand → agent 或 respond
    builder.add_conditional_edges(
        "understand",
        route_after_understand,
        {"agent": "agent", "respond": "respond"},
    )

    # agent → tools / guard / respond
    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "guard": "guard", "respond": "respond"},
    )

    # ★ 这条边构成了 Agent 的核心循环：
    #   执行完工具 → 回到 agent 让它看结果、决定下一步
    #
    # 循环的终止由 route_after_agent 保证：
    #   模型不再调工具、或触发了步数/token 上限，就会走向 respond。
    # 没有这个保证，模型可能陷入"查了又查"的死循环。
    builder.add_edge("tools", "agent")

    # guard 之后图就结束了 —— 计划已经落库，等用户确认。
    # 确认后的执行走另一个入口（app/agent/executor.py）。
    builder.add_edge("guard", END)

    builder.add_edge("respond", END)

    return builder


# 编译后的图。模块级单例，进程内共用。
#
# 注意：这里【没有】传 checkpointer。
# 我们用的是"两阶段"方案（计划落 MySQL），不依赖图的持久化状态，
# 详见 app/agent/runner.py 顶部的说明。
agent_graph = build_graph().compile()


__all__ = ["agent_graph", "build_graph", "route_after_agent", "route_after_understand"]
