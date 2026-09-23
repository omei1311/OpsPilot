"""批量工具（场景 4）。

⚠️ 这两个工具【永远不会被直接执行】。

它们出现在 policy.ALWAYS_REQUIRE_APPROVAL 里，风险等级恒为 high。
Agent 调它们时，系统不会真的去改数据，而是：
    ① 把调用意图变成一份"执行计划"
    ② 写进 agent_pending_actions 表
    ③ 通过 SSE 推给前端，弹出确认卡片
    ④ 等用户点了"确认"才真正执行（见 agent/executor.py）

换句话说：**这两个工具的"调用"和"执行"是分开的**。
这是 Human-in-the-loop 在代码层面的落地方式。
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.context import ToolContext


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


@tool
async def batch_update_tickets(
    ticket_ids: list[int],
    priority: str | None = None,
    category: str | None = None,
    department_id: int | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """批量修改多张工单。用户要求"把所有符合条件的工单改成..."时使用。

    ⚠️ 这个操作需要用户确认后才会真正执行。
    你调用它之后，系统会自动向用户展示确认框，你不需要自己写"请确认"。

    ⚠️ ticket_ids 必须是【真实存在的工单 ID】。
    调用前必须先用 list_tickets 查出符合条件的工单，从返回结果里取 id 字段。
    绝对不要自己编造 ID。

    Args:
        ticket_ids: 工单 ID 列表（不是工单号）。从 list_tickets 的返回里取。
        priority: 要设置成的新优先级。可选 low/medium/high/urgent。
        category: 要设置成的新分类。
        department_id: 要分配到的部门 ID。先用 list_departments 查。

    Returns:
        这个工具本身不会真的修改数据，只返回"已提交确认"的提示。
        真正的执行发生在用户确认之后。
    """
    # 这里只做参数校验和拦截，不执行任何写操作。
    # 真正的执行入口是 app/agent/executor.py，由审批接口触发。
    ctx = ToolContext.from_config(config)

    if not ticket_ids:
        return _fail("ticket_ids 不能为空。请先用 list_tickets 查询出符合条件的工单")

    if priority is None and category is None and department_id is None:
        return _fail("至少要指定一个要修改的字段（priority / category / department_id）")

    if not ctx.is_operator:
        return _fail("当前角色没有批量修改工单的权限")

    # 返回一个"等待确认"的标记。runner 会识别这个标记并走 HITL 流程。
    return {
        "ok": True,
        "pending_approval": True,
        "tool": "batch_update_tickets",
        "ticket_ids": ticket_ids,
        "changes": {
            k: v
            for k, v in {
                "priority": priority,
                "category": category,
                "department_id": department_id,
            }.items()
            if v is not None
        },
        "affected_count": len(ticket_ids),
    }


@tool
async def batch_assign_tickets(
    ticket_ids: list[int],
    department_id: int | None = None,
    assignee_id: int | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """批量分派多张工单给某个部门或某个人。

    ⚠️ 这个操作需要用户确认后才会真正执行。

    ⚠️ ticket_ids 必须是【真实存在的工单 ID】，先用 list_tickets 查出。
    ⚠️ 用户说的"技术部门"是中文名，必须先用 list_departments 换成 department_id。

    Args:
        ticket_ids: 工单 ID 列表（不是工单号）。
        department_id: 目标部门 ID。
        assignee_id: 目标负责人 ID（如果指定到人）。

    Returns:
        工具本身不修改数据，只返回"已提交确认"的提示。
    """
    ctx = ToolContext.from_config(config)

    if not ticket_ids:
        return _fail("ticket_ids 不能为空。请先用 list_tickets 查询出符合条件的工单")

    if department_id is None and assignee_id is None:
        return _fail("必须指定 department_id 或 assignee_id")

    if not ctx.is_operator:
        return _fail("当前角色没有批量分派工单的权限")

    return {
        "ok": True,
        "pending_approval": True,
        "tool": "batch_assign_tickets",
        "ticket_ids": ticket_ids,
        "changes": {
            k: v
            for k, v in {"department_id": department_id, "assignee_id": assignee_id}.items()
            if v is not None
        },
        "affected_count": len(ticket_ids),
    }
