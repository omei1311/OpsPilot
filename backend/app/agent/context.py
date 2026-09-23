"""工具上下文。

★★★ 这是整个 Agent 安全模型的地基。 ★★★

问题：Agent 工具需要知道"当前是谁在操作"，才能做权限校验。
但如果把 user_id 做成工具的一个【参数】，会发生什么？

    # ❌ 危险的写法
    @tool
    async def list_tickets(user_id: int, status: str = None):
        ...

    LLM 看到的工具签名里就有 user_id，于是它可能：
      · 猜错用户（把 3 写成 4）
      · 被提示注入攻击利用（用户说"以 admin 的身份查"）
      · 调用时干脆编一个不存在的 id

    提示词里写一万句"不要伪造 user_id"都没用 —— 模型是可以被绕过的。

正确做法：把身份信息放在工具【签名之外】的运行时上下文里。

    # ✅ 安全的写法
    @tool
    async def list_tickets(status: str = None, config: RunnableConfig = None):
        ctx = ToolContext.from_config(config)   # ← 身份从后端注入，LLM 碰不到

LangChain 会自动把声明了 `config: RunnableConfig` 的参数
从工具的 JSON Schema 里剔除 —— 所以 LLM 既看不到它，也无法提供它。
身份由后端在调用图的时候放进 config，一路传到工具里。

这样"以谁的身份执行"就变成了一个【代码决定的事实】，
而不是一个【模型可以填的参数】。这是 Tool Calling 安全的核心。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError
from app.models.user import User, UserRole

# 在 config 里存 ToolContext 用的 key
CONTEXT_KEY = "tool_context"


@dataclass
class ToolContext:
    """一次工具调用所需的运行时环境。

    注意这里【只有】后端能提供的东西：
        db      —— 数据库会话，由 FastAPI 依赖注入提供
        user    —— 当前登录用户，由 JWT 解析得到
        run_id  —— 本次 Agent 运行 ID，用于把工具调用和审计日志关联起来

    没有任何一项来自 LLM。
    """

    db: AsyncSession
    user: User
    run_id: str | None = None
    # 已经调用过的工具记录，供 policy 层判断"是否重复调用"
    call_history: list[str] = field(default_factory=list)

    # ── 权限便捷方法 ───────────────────────────────────────
    #
    # 这些只是【给工具快速判断用】的辅助方法。
    # 真正的数据过滤仍然在 Service 层（TicketService._apply_scope），
    # 因为那才是不可绕过的地方 —— 工具层忘了调这里，Service 也拦得住。

    @property
    def is_admin(self) -> bool:
        return self.user.role == UserRole.ADMIN

    @property
    def is_operator(self) -> bool:
        return self.user.role in (UserRole.ADMIN, UserRole.OPERATOR)

    def require_operator(self, action: str) -> None:
        """要求操作员及以上角色，否则抛 403。"""
        if not self.is_operator:
            raise ForbiddenError(
                f"当前角色无权执行「{action}」",
                detail={"required": "admin 或 operator", "your_role": self.user.role},
            )

    # ── 序列化 ────────────────────────────────────────────

    def to_config(self) -> dict[str, Any]:
        """打包成 RunnableConfig，传给 graph.astream()。"""
        return {"configurable": {CONTEXT_KEY: self}}

    @staticmethod
    def from_config(config: RunnableConfig | None) -> ToolContext:
        """从 RunnableConfig 里取回 ToolContext。

        每个工具的第一件事都是调用它。取不到就直接报错 ——
        取不到说明调用链有问题（比如忘了通过 graph 调用而直接调了工具），
        这种情况必须立刻暴露，不能带着"没有身份"的状态继续跑。
        """
        if config is None:
            raise RuntimeError("工具调用缺少 config，无法确定执行身份")

        configurable = config.get("configurable") or {}
        ctx = configurable.get(CONTEXT_KEY)

        if not isinstance(ctx, ToolContext):
            raise RuntimeError(
                "工具调用缺少 ToolContext。"
                "请确认是通过 graph.astream(..., config=ctx.to_config()) 调用的工具。"
            )
        return ctx
