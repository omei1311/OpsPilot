"""Agent 运行相关模型。

五张表，各自解决一个问题：

    agent_conversations   会话（一轮对话的容器）
    agent_messages        消息历史（多轮对话的上下文）
    agent_runs            一次执行（可观测、可统计）
    agent_run_steps       执行明细（前端时间线的数据源）
    agent_pending_actions 待确认操作（Human-in-the-loop 的核心）

最后一张是重点 —— 它是"人工确认"这个动作的持久化载体。
没有它，确认流程只能活在内存里，服务一重启就丢。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPKMixin, TimestampMixin


# ══════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════


class AgentRunStatus(StrEnum):
    """一次 Agent 运行的状态。"""

    RUNNING = "running"                      # 正在执行
    AWAITING_APPROVAL = "awaiting_approval"  # 等待人工确认
    SUCCESS = "success"                      # 成功完成
    REJECTED = "rejected"                    # 用户拒绝了操作
    FAILED = "failed"                        # 执行出错
    CANCELLED = "cancelled"                  # 用户主动取消 / 连接断开


class AgentStepType(StrEnum):
    """执行明细的类型。"""

    INTENT = "intent"              # 意图识别结果
    THOUGHT = "thought"            # 模型思考文本
    TOOL_CALL = "tool_call"        # 工具调用
    TOOL_RESULT = "tool_result"    # 工具返回
    PLAN = "plan"                  # 生成计划
    APPROVAL = "approval"          # 人工审批结果
    ANSWER = "answer"              # 最终回答
    ERROR = "error"                # 出错


class PendingActionStatus(StrEnum):
    """待确认操作的状态机。

        pending ──→ approved ──→ executed
           │            └─────→ failed
           ├──→ rejected
           └──→ expired
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


# ══════════════════════════════════════════════════════════════
# 会话
# ══════════════════════════════════════════════════════════════


class AgentConversation(Base, BigIntPKMixin, TimestampMixin):
    """一次会话。

    一个会话包含多次运行（run）：
        用户："有哪些高优先级工单？"   → run 1
        用户："把第一条分派给技术部"   → run 2（同一会话，带上下文）
    """

    __tablename__ = "agent_conversations"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True,
        comment="会话归属用户",
    )
    title: Mapped[str] = mapped_column(
        String(100), nullable=False, default="新对话", comment="会话标题"
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="最后活跃时间，用于会话列表排序"
    )

    def __repr__(self) -> str:
        return f"<AgentConversation id={self.id} user={self.user_id}>"


class AgentMessage(Base, BigIntPKMixin, TimestampMixin):
    """对话消息（多轮上下文）。

    只存 user 和 assistant 两类 —— 工具调用和结果存在
    agent_run_steps 里，不混进来。原因是：
      · 消息历史要注入下一轮的提示词，越短越好
      · 工具原始返回值可能很大（几十条工单），塞进历史会撑爆上下文
    """

    __tablename__ = "agent_messages"

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_conversations.id"), nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="user / assistant"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息内容")
    run_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="这条消息由哪次运行产生"
    )

    __table_args__ = (
        Index("ix_agent_messages_conv_created", "conversation_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<AgentMessage conv={self.conversation_id} role={self.role}>"


# ══════════════════════════════════════════════════════════════
# 运行
# ══════════════════════════════════════════════════════════════


class AgentRun(Base, BigIntPKMixin, TimestampMixin):
    """一次 Agent 执行。

    run_id 是贯穿全链路的追踪标识：
      · SSE 事件里带着它
      · agent_run_steps 里带着它
      · 工单的审计日志（ticket_logs）也会记录它
    这样"AI 改了哪些数据"就能完整回溯。
    """

    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, nullable=False, comment="UUID"
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_conversations.id"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True,
    )

    user_input: Mapped[str] = mapped_column(Text, nullable=False, comment="用户输入原文")
    intent: Mapped[str | None] = mapped_column(
        String(30), nullable=True, comment="识别出的意图"
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=AgentRunStatus.RUNNING, index=True,
        comment="running/awaiting_approval/success/rejected/failed/cancelled",
    )

    answer: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最终回答")
    error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")

    # token 用量：用来观察成本，也方便面试时回答"怎么控制 token 消耗"
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="输入 token"
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="输出 token"
    )
    tool_call_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="工具调用次数"
    )
    duration_ms: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="总耗时(毫秒)"
    )

    def __repr__(self) -> str:
        return f"<AgentRun {self.run_id} {self.status}>"


class AgentRunStep(Base, BigIntPKMixin, TimestampMixin):
    """执行明细的一步。

    ★ 为什么单独一张表，而不是塞进 agent_runs 的一个 JSON 字段？

      1. 前端刷新后要能【回放执行过程】，这需要按 run_id 分页查询
      2. 将来想统计"哪个工具最慢""哪个工具最常失败"，
         独立表可以直接 GROUP BY，JSON 得全表扫再解析
      3. 执行过程可能有几十步，全部塞一个字段会让那一行变得很胖

    这是"可观测性"的落地方式 —— 用户能看见 AI 到底做了什么，
    而不是只看到一个最终答案。
    """

    __tablename__ = "agent_run_steps"

    run_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True, comment="关联 agent_runs.run_id"
    )
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="步骤序号，从 1 开始"
    )
    step_type: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="intent/thought/tool_call/tool_result/plan/approval/answer/error",
    )
    node: Mapped[str | None] = mapped_column(
        String(30), nullable=True, comment="产生这一步的图节点名"
    )
    tool_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ok", comment="ok / failed"
    )
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (Index("ix_agent_steps_run_seq", "run_id", "seq"),)

    def __repr__(self) -> str:
        return f"<AgentRunStep {self.run_id}#{self.seq} {self.step_type}>"


# ══════════════════════════════════════════════════════════════
# 待确认操作（HITL 核心）
# ══════════════════════════════════════════════════════════════


class AgentPendingAction(Base, BigIntPKMixin, TimestampMixin):
    """等待人工确认的操作。

    ★ 这是 Human-in-the-loop 的持久化载体。

    为什么落库而不是放在图的状态里？
      · 用户可能几分钟后才点确认，期间服务可能重启
      · 多 worker 部署时，确认请求不一定打到当初那个进程
      · 需要能被查询（前端显示"有 2 个待确认"红点）
      · 需要能超时（定时任务扫描 expires_at）

    落库之后，"等待确认"就从"某个进程的内存状态"
    变成了"一条可查询、可过期、可审计的数据库记录"。
    """

    __tablename__ = "agent_pending_actions"

    action_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, nullable=False,
        comment="对外暴露的操作 ID（UUID），不用主键是为了不泄露业务量级",
    )
    run_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True, comment="关联 agent_runs.run_id"
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_conversations.id"), nullable=False,
        comment="确认后要回到哪个会话继续",
    )
    requested_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True,
        comment="发起人",
    )

    title: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="给用户看的一句话描述"
    )
    risk_level: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="low/medium/high"
    )
    # 完整的执行计划。确认时如果用户改了勾选，会用 edited_payload 覆盖
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="执行计划（工具名 + 参数 + 影响范围）"
    )
    affected_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="影响行数"
    )
    # 受影响对象的摘要，用于前端预览（展示具体是哪些工单，而不是"17 条"）
    preview: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="受影响对象摘要"
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PendingActionStatus.PENDING, index=True,
        comment="pending/approved/rejected/expired/executed/failed",
    )
    decided_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True, comment="审批人"
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="审批意见 / 拒绝原因"
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True, comment="超时时间，过期后不可再确认"
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="执行结果明细（含失败清单）"
    )

    __table_args__ = (
        # 定时任务扫"哪些过期了"用的索引
        Index("ix_pending_status_expires", "status", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<PendingAction {self.action_id} {self.status}>"
