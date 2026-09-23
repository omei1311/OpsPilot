"""SLA 服务：计算截止时间、判断风险等级。

这个文件回答了三个问题：
    1. 新建工单时，截止时间应该是几点？        → calc_deadline
    2. 现在这张工单是正常 / 快超时 / 已超时？   → assess
    3. 超时了多少张？                          → 由 TicketService 聚合调用
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.sla import SlaPolicy
from app.models.ticket import TicketPriority

logger = get_logger(__name__)


class SlaService:
    """SLA 计算。

    方法都是 staticmethod：不需要实例状态，直接 SlaService.xxx() 调用。
    """

    # ── 兜底策略 ────────────────────────────────────────────
    #
    # 数据库里的 sla_policies 表如果没数据（比如忘了跑种子脚本），
    # 用这一份兜底，保证工单永远能算出截止时间，不会因为缺配置就建不出来。
    #
    # 格式：优先级 → (响应时限分钟, 解决时限分钟)
    FALLBACK_POLICIES: dict[str, tuple[int, int]] = {
        TicketPriority.URGENT: (15, 240),     # 15 分钟响应，4 小时解决
        TicketPriority.HIGH: (60, 480),       # 1 小时响应，8 小时解决
        TicketPriority.MEDIUM: (240, 1440),   # 4 小时响应，24 小时解决
        TicketPriority.LOW: (480, 2880),      # 8 小时响应，48 小时解决
    }

    # SLA 剩余时间低于总时限的这个比例时，标记为"有风险"
    AT_RISK_RATIO = 0.2

    @staticmethod
    async def get_policy(db: AsyncSession, priority: str) -> SlaPolicy | None:
        """按优先级取 SLA 策略。查不到返回 None。"""
        stmt = select(SlaPolicy).where(
            SlaPolicy.priority == priority,
            SlaPolicy.is_active.is_(True),
        )
        return (await db.execute(stmt)).scalars().first()

    @staticmethod
    async def calc_deadline(
        db: AsyncSession,
        priority: str,
        *,
        base_time: datetime | None = None,
    ) -> datetime:
        """算出这张工单的 SLA 截止时间。

        Args:
            priority:  优先级，决定用哪条策略
            base_time: 起算时间。默认当前时间。
                       修改优先级时会重新调用本方法，那时传入原始创建时间，
                       表示"按新优先级重新计算，但时间从创建时刻开始算"。

        Returns:
            截止时间（UTC 朴素时间，和数据库存储格式一致）
        """
        policy = await SlaService.get_policy(db, priority)

        if policy is not None:
            minutes = policy.resolution_minutes
        else:
            # 查不到就用兜底值，同时打条日志提醒
            minutes = SlaService.FALLBACK_POLICIES.get(priority, (240, 1440))[1]
            logger.warning(
                "SLA 策略缺失，使用兜底值 | priority=%s resolution=%smin",
                priority,
                minutes,
            )

        start = base_time or datetime.now(timezone.utc).replace(tzinfo=None)
        return start + timedelta(minutes=minutes)

    @staticmethod
    def assess(
        *,
        created_at: datetime,
        sla_deadline: datetime | None,
        status: str,
        now: datetime | None = None,
    ) -> tuple[str, int | None]:
        """判断 SLA 状态和剩余时间。

        Returns:
            (sla_status, remaining_minutes)
            sla_status 取值：
                done    已解决/已关闭，不再计算
                overdue 已超时
                at_risk 快超时了（剩余不足总时限的 20%）
                normal  正常
            remaining_minutes 为负表示已超时多少分钟
        """
        # 已结束的工单不再有 SLA 压力
        if status in ("resolved", "closed"):
            return "done", None

        # 没设截止时间（理论上不该发生）就当作正常
        if sla_deadline is None:
            return "normal", None

        current = now or datetime.now(timezone.utc).replace(tzinfo=None)
        remaining = int((sla_deadline - current).total_seconds() // 60)

        if remaining <= 0:
            return "overdue", remaining

        # 用"总时限"算比例：剩余时间不足总时限 20% 就算有风险。
        # 不能用一个固定分钟数（比如"剩 2 小时"）判断 ——
        # 对 4 小时时限的紧急工单来说 2 小时已经很紧张，
        # 但对 48 小时时限的低优先级工单来说完全不用慌。
        total_minutes = (sla_deadline - created_at).total_seconds() / 60
        if total_minutes > 0 and remaining < total_minutes * SlaService.AT_RISK_RATIO:
            return "at_risk", remaining

        return "normal", remaining
