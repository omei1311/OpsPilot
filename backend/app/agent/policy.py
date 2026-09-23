"""工具调用策略：风险分级 + 是否需人工确认。

★★★ 这个文件里【一行 LLM 调用都没有】，这是刻意的。 ★★★

安全决策不能交给模型。如果让模型自己判断"我这个操作危不危险"，
那等于让嫌疑人自己决定要不要被搜身 —— 提示注入、模型幻觉、
上下文误导都能绕过它。所以风险判定必须是确定性的代码逻辑。

判据只有三条：
    ① 影响几行数据？          —— 多行就要确认
    ② 可不可逆？              —— 进入终态、提升优先级要确认
    ③ 操作者有没有权限？      —— 没有直接拒绝

命中任意一条 → 必须人工确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.core.config import settings

RiskLevel = Literal["low", "medium", "high", "unknown"]


# ══════════════════════════════════════════════════════════════
# 工具分组
# ══════════════════════════════════════════════════════════════

# 只读工具：无副作用，永远直接执行
READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_tickets",
        "get_ticket",
        "get_ticket_statistics",
        "analyze_sla_risk",
        "summarize_ticket",
        "list_departments",
    }
)

# 写工具：有副作用，按规则分级
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_ticket",
        "update_ticket",
        "assign_ticket",
        "change_ticket_status",
        "add_ticket_comment",
        # 批量工具（阶段 10 加入），永远高风险
        "batch_update_tickets",
        "batch_assign_tickets",
    }
)

# 无条件需要人工确认的工具
ALWAYS_REQUIRE_APPROVAL: frozenset[str] = frozenset(
    {
        "batch_update_tickets",
        "batch_assign_tickets",
    }
)

# 进入这些状态是不可逆的（closed 是终态，cancelled 同理）
IRREVERSIBLE_STATUSES: frozenset[str] = frozenset({"closed"})

# 优先级高到低。用来判断"这次改动是不是升级"
PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2, "urgent": 3}


@dataclass
class RiskAssessment:
    """一次工具调用的风险评估结果。"""

    tool_name: str
    risk_level: RiskLevel
    requires_approval: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
        }


def assess_risk(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    before: dict[str, Any] | None = None,
) -> RiskAssessment:
    """评估一次工具调用的风险。

    Args:
        tool_name: 工具名
        args:      本次调用的参数
        before:    修改前的现状（比如工单当前的优先级），
                   用于判断"改优先级"到底是升级还是降级。

    Returns:
        RiskAssessment
    """
    args = args or {}

    # ── 只读工具：永远安全 ─────────────────────────────────
    if tool_name in READ_TOOLS:
        return RiskAssessment(tool_name, "low", False, "只读操作，无副作用")

    # ── 未知工具：按最严格处理 ─────────────────────────────
    #
    # 这个默认分支很重要。将来有人加了新工具却忘了在这里登记，
    # 不会因为"没规则"就被放行，而是直接被要求人工确认。
    # 安全设计里这叫 fail-safe（故障安全），默认拒绝而不是默认允许。
    if tool_name not in WRITE_TOOLS:
        return RiskAssessment(
            tool_name, "unknown", True, f"未登记的工具 {tool_name}，按最严格策略处理"
        )

    # ── 无条件确认 ─────────────────────────────────────────
    if tool_name in ALWAYS_REQUIRE_APPROVAL:
        count = len(args.get("ticket_ids") or [])
        return RiskAssessment(
            tool_name,
            "high",
            True,
            f"批量操作，影响 {count} 条数据，必须人工确认",
        )

    # ── 创建工单：低风险 ───────────────────────────────────
    #
    # 为什么创建不需要确认？
    #   新增一条数据不会破坏已有数据，而且可以撤销（改成 closed 即可）。
    #   如果连"帮我报个故障"都要点确认，体验会很糟糕 ——
    #   确认疲劳会让人条件反射地点"同意"，反而削弱了 HITL 的价值。
    if tool_name == "create_ticket":
        return RiskAssessment(tool_name, "low", False, "新增数据，不影响已有记录，可撤销")

    # ── 改优先级：升级要确认 ───────────────────────────────
    if tool_name == "update_ticket" and "priority" in args:
        new_priority = args.get("priority")
        old_priority = (before or {}).get("priority")

        if _is_upgrade(old_priority, new_priority):
            return RiskAssessment(
                tool_name,
                "high",
                True,
                f"升级优先级 {old_priority} → {new_priority}，会改变 SLA 时限并影响排期",
            )
        return RiskAssessment(
            tool_name, "medium", False, f"调整优先级 {old_priority} → {new_priority}"
        )

    # ── 变更状态：进终态要确认 ─────────────────────────────
    if tool_name == "change_ticket_status":
        target = args.get("status")
        if target in IRREVERSIBLE_STATUSES:
            return RiskAssessment(
                tool_name,
                "high",
                True,
                f"变更为「{target}」是终态操作，之后无法再流转",
            )
        return RiskAssessment(tool_name, "medium", False, f"状态变更为「{target}」")

    # ── 其余写操作：中等风险，直接执行 ─────────────────────
    if tool_name in {"assign_ticket", "add_ticket_comment"}:
        return RiskAssessment(tool_name, "medium", False, "单条数据的常规修改")

    # 兜底：理论上走不到（前面已经拦掉了未登记工具），但保持默认安全
    return RiskAssessment(tool_name, "high", True, "未匹配到规则的写操作，按最严格处理")


def _is_upgrade(old: Any, new: Any) -> bool:
    """判断优先级是不是被提高了。

    无法判断时（比如没有 before 信息）返回 True —— 宁可多问一次，
    也不要漏掉一次该确认的升级。
    """
    if old is None or new is None:
        return True
    old_rank = PRIORITY_ORDER.get(str(old))
    new_rank = PRIORITY_ORDER.get(str(new))
    if old_rank is None or new_rank is None:
        return True
    return new_rank > old_rank


def check_batch_limit(count: int) -> tuple[bool, str]:
    """检查批量操作是否超过安全阀值。

    防止模型一次改几千条。即使有人确认，数量太大也应该拦住 ——
    人工确认在数量面前会失效（没人会逐条核对 5000 条）。
    """
    limit = settings.AGENT_BATCH_LIMIT
    if count > limit:
        return False, f"单次批量操作最多 {limit} 条，本次尝试修改 {count} 条"
    return True, ""
