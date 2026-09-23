"""Agent 工具注册表。

这里集中管理：有哪些工具、每个工具的风险等级、不同意图下给模型看哪些工具。

★ 为什么要有"意图 → 工具白名单"？

    假设 13 个工具全部暴露给模型，用户问"帮我看看最近的工单"，
    模型面对 13 个选项，选错的概率明显上升。

    如果按意图收窄到 3 个（list_tickets / get_ticket / list_departments），
    模型几乎不可能选错。

    这是"用工程手段减少模型出错空间"的典型做法 ——
    与其在提示词里千叮咛万嘱咐，不如从源头把选项变少。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.agent.tools.batch_tools import batch_assign_tickets, batch_update_tickets
from app.agent.tools.read_tools import (
    analyze_sla_risk,
    get_ticket,
    get_ticket_statistics,
    list_departments,
    list_tickets,
    summarize_ticket,
)
from app.agent.tools.write_tools import (
    add_ticket_comment,
    assign_ticket,
    change_ticket_status,
    create_ticket,
    update_ticket,
)

# ══════════════════════════════════════════════════════════════
# 全部工具
# ══════════════════════════════════════════════════════════════

ALL_TOOLS: list[BaseTool] = [
    # 读
    list_tickets,
    get_ticket,
    get_ticket_statistics,
    analyze_sla_risk,
    summarize_ticket,
    list_departments,
    # 写（单条）
    create_ticket,
    update_ticket,
    assign_ticket,
    change_ticket_status,
    add_ticket_comment,
    # 写（批量）★ 这两个不会直接执行，会被 guard 节点拦下来走人工确认
    batch_update_tickets,
    batch_assign_tickets,
]

# 按名字索引，方便按名字取工具
TOOL_MAP: dict[str, BaseTool] = {t.name: t for t in ALL_TOOLS}


# ══════════════════════════════════════════════════════════════
# 意图 → 工具白名单
# ══════════════════════════════════════════════════════════════

# 每个意图只暴露必要的工具，减少模型选错的概率
INTENT_TOOLS: dict[str, list[str]] = {
    # 闲聊 / 兜底：不给任何工具，直接回答
    "chat": [],
    # 场景 1：创建工单
    "create_ticket": [
        "list_departments",
        "create_ticket",
    ],
    # 场景 2：查询工单
    "query_ticket": [
        "list_tickets",
        "get_ticket",
        "summarize_ticket",
        "list_departments",
    ],
    # 场景 3：SLA 风险分析
    "analyze": [
        "list_tickets",
        "get_ticket_statistics",
        "analyze_sla_risk",
        "summarize_ticket",
    ],
    # 场景 4：批量修改
    #
    # ★ 这个白名单是场景 4 能成立的关键：
    #   模型只可能选到这三个工具，其中两个是批量工具（会被 guard 拦截，
    #   走人工确认），另一个是 list_tickets（用来查候选集）。
    #   它没有机会"顺手"去调 create_ticket 或别的东西。
    "batch_update": [
        "list_tickets",
        "list_departments",
        "batch_update_tickets",
        "batch_assign_tickets",
    ],
    # 修改单张工单
    "update_ticket": [
        "get_ticket",
        "list_tickets",
        "list_departments",
        "update_ticket",
        "assign_ticket",
        "change_ticket_status",
        "add_ticket_comment",
    ],
}


def get_tools_for_intent(intent: str) -> list[BaseTool]:
    """按意图取工具列表。

    意图不认识时返回【空列表】而不是全部工具 ——
    fail-safe：宁可让模型说"我不太确定该怎么做"，
    也不要让它在不确定的意图下拿到写工具。
    """
    names = INTENT_TOOLS.get(intent, [])
    return [TOOL_MAP[n] for n in names if n in TOOL_MAP]


__all__ = [
    "ALL_TOOLS",
    "TOOL_MAP",
    "INTENT_TOOLS",
    "get_tools_for_intent",
]
