"""Agent State 设计。

State 是 LangGraph 里在节点之间流动的"共享数据包"。
每个节点读它、改它，然后传给下一个节点。

设计原则：
    ① 只放【跨节点需要共享】的东西。节点内部的临时变量不要塞进来
    ② 输入上下文（身份、会话）和图执行过程（消息、步数）分开标注，
       看代码时一眼能分清"哪些是外面传进来的、哪些是图自己产生的"
    ③ 循环保护字段必须有（step_count），否则模型可能无限调工具
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════
# 结构化输出的模型
# ══════════════════════════════════════════════════════════════


class IntentResult(BaseModel):
    """意图识别的结构化输出。

    用 Literal 而不是 str 很关键：
      · 用 str，模型可能返回"上报故障""创建工单""create"等自由发挥的值，
        后续路由就对不上了
      · 用 Literal，模型被约束在固定选项里，路由代码可以放心写 if/elif
    """

    intent: Literal[
        "create_ticket",   # 想创建工单
        "query_ticket",    # 想查询工单
        "analyze",         # 想做分析（统计、SLA 风险）
        "batch_update",    # 想批量修改（场景 4）
        "update_ticket",   # 想修改单张工单
        "chat",            # 闲聊或与工单无关
    ] = Field(description="用户意图")
    confidence: float = Field(ge=0, le=1, description="置信度 0-1")
    reasoning: str = Field(description="判定理由，一句话")
    entities: dict[str, Any] = Field(
        default_factory=dict,
        description="从用户输入中抽取的关键信息，如标题、分类、优先级、时间范围、部门名称",
    )


class PlanStep(BaseModel):
    """批量执行计划中的一步。"""

    seq: int = Field(description="步骤序号")
    tool: str = Field(description="要调用的工具名")
    args: dict[str, Any] = Field(description="工具参数")
    description: str = Field(description="给用户看的中文说明")
    affected_count: int = Field(default=0, description="影响行数")


class ExecutionPlan(BaseModel):
    """一个完整的批量执行计划。"""

    title: str = Field(description="一句话概括这次要做什么")
    steps: list[PlanStep] = Field(default_factory=list)
    affected_count: int = Field(default=0, description="总影响行数")
    risk_level: Literal["low", "medium", "high"] = "high"
    preview: list[dict[str, Any]] = Field(
        default_factory=list, description="受影响对象的摘要，给用户在确认卡片里核对"
    )


# ══════════════════════════════════════════════════════════════
# 图状态
# ══════════════════════════════════════════════════════════════


class AgentState(TypedDict, total=False):
    """在 LangGraph 各节点之间流转的状态。

    用 total=False 表示所有字段都是可选的 —— 因为状态是一步步填充起来的，
    刚进图的时候只有最上面那几个输入字段。
    """

    # ── ① 输入上下文（图的入口一次性注入，之后只读）──────────
    run_id: str
    conversation_id: int
    user_id: int
    user_role: str
    user_input: str

    # ── ② 理解结果 ─────────────────────────────────────────
    intent: str
    intent_confidence: float
    entities: dict[str, Any]

    # ── ③ 执行过程 ─────────────────────────────────────────
    #
    # messages 用 add_messages 归约器：节点返回的新消息会被【追加】
    # 到列表末尾，而不是覆盖整个列表。
    # 不写这个 Annotated 的话，每个节点返回 messages 都会把历史冲掉。
    messages: Annotated[list[BaseMessage], add_messages]

    # 本次运行可用工具的白名单（由 intent 决定）
    available_tools: list[str]

    # 已经执行过的工具调用记录：[{tool, args, ok, summary}]
    observations: list[dict[str, Any]]

    # ── ④ 待确认（HITL）─────────────────────────────────────
    plan: dict[str, Any] | None          # ExecutionPlan 序列化后的字典
    pending_action_id: str | None        # agent_pending_actions.action_id
    approval: dict[str, Any] | None      # 审批结果（恢复执行时注入）

    # ── ⑤ 循环保护与统计 ───────────────────────────────────
    step_count: int
    total_tokens: int
    affected_ticket_ids: list[int]

    # ── ⑥ 输出 ─────────────────────────────────────────────
    answer: str
    citations: list[dict[str, Any]]
    error: str | None
    status: str


def initial_state(
    *,
    run_id: str,
    conversation_id: int,
    user_id: int,
    user_role: str,
    user_input: str,
    history: list[BaseMessage] | None = None,
) -> AgentState:
    """构造图的初始状态。

    这样写而不是让调用方手拼字典的好处：
    初始状态该有哪些字段是明确的，少了会很难查。
    """
    return AgentState(
        run_id=run_id,
        conversation_id=conversation_id,
        user_id=user_id,
        user_role=user_role,
        user_input=user_input,
        messages=history or [],
        available_tools=[],
        observations=[],
        plan=None,
        pending_action_id=None,
        approval=None,
        step_count=0,
        total_tokens=0,
        affected_ticket_ids=[],
        answer="",
        citations=[],
        error=None,
        status="running",
    )
