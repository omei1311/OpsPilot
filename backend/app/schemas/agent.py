"""Agent 相关出入参。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.agent import AgentRunStatus, PendingActionStatus
from app.schemas.common import UTCDatetime

# ══════════════════════════════════════════════════════════════
# 入参
# ══════════════════════════════════════════════════════════════


class ChatRequest(BaseModel):
    """发消息。

    注意这里【没有 user_id】—— 身份一律从 JWT 取。
    如果让前端传 user_id，任何人都能冒充别人跟 AI 对话并操作数据。
    """

    message: str = Field(min_length=1, max_length=2000, description="用户输入")
    conversation_id: int | None = Field(
        default=None, description="会话 ID。不传则新建一个会话"
    )


class ApproveRequest(BaseModel):
    """确认执行。

    edited_payload 让用户可以在确认时改勾选 ——
    比如 Agent 找到 17 条，用户取消勾选其中 2 条。
    """

    edited_payload: dict[str, Any] | None = Field(
        default=None,
        description="修改后的执行参数，如 {'ticket_ids': [1,2,3]}。不传则按原计划执行",
    )
    note: str | None = Field(default=None, max_length=500, description="备注")


class RejectRequest(BaseModel):
    """拒绝执行。"""

    note: str | None = Field(default=None, max_length=500, description="拒绝原因")


# ══════════════════════════════════════════════════════════════
# 出参
# ══════════════════════════════════════════════════════════════


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    last_active_at: UTCDatetime
    created_at: UTCDatetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    run_id: str | None = None
    created_at: UTCDatetime


class RunStepOut(BaseModel):
    """执行明细的一步，前端时间线的数据源。"""

    model_config = ConfigDict(from_attributes=True)

    seq: int
    step_type: str
    node: str | None = None
    tool_name: str | None = None
    input_data: dict[str, Any] | None = None
    output_data: dict[str, Any] | None = None
    status: str
    duration_ms: int | None = None
    created_at: UTCDatetime


class RunOut(BaseModel):
    """一次运行的完整信息（用于刷新后回放）。"""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    conversation_id: int
    intent: str | None = None
    status: AgentRunStatus
    user_input: str
    answer: str | None = None
    error: str | None = None
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int | None = None
    created_at: UTCDatetime
    steps: list[RunStepOut] = Field(default_factory=list)


class PendingActionOut(BaseModel):
    """待确认操作。

    前端拿它渲染"确认卡片"：标题、步骤、影响条数、逐条预览、倒计时。
    """

    model_config = ConfigDict(from_attributes=True)

    action_id: str
    run_id: str
    title: str
    risk_level: str
    affected_count: int
    preview: list[dict[str, Any]] | None = None
    status: PendingActionStatus
    expires_at: UTCDatetime
    created_at: UTCDatetime


class PendingActionDetail(PendingActionOut):
    """待确认操作的完整信息（含执行参数，用于确认页展示）。"""

    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    decided_at: UTCDatetime | None = None
    decision_note: str | None = None
