"""工单操作记录服务。

单独一个文件的原因：评论、改状态、分派、Agent 批量修改……
都要写操作记录。集中在这里，格式统一，不会有的地方写有的地方忘。

⚠️ 这个服务只提供"写"和"读"，**没有 update / delete**。
操作记录一旦写入就不可篡改 —— 否则审计功能就失去意义了。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.ticket import TicketEvent

logger = get_logger(__name__)


class EventService:
    """工单操作记录。"""

    @staticmethod
    async def record(
        db: AsyncSession,
        *,
        ticket_id: int,
        user_id: int | None,
        event_type: str,
        event_data: dict[str, Any] | None = None,
        flush: bool = False,
    ) -> TicketEvent:
        """写一条操作记录。

        Args:
            flush: 是否立刻 flush。

        关于 flush 和 commit 的区别（面试常问）：
            flush()   把 SQL 发给数据库，但【不提交事务】。
                      别人看不到，你也可以随时回滚。
            commit()  提交事务，数据真正落盘、别人可见。

        为什么默认 flush=False？
            因为大多数场景下，这条记录要和工单本身的修改
            【在同一个事务里】一起提交。由 TicketService 统一 commit，
            这样才能保证"工单改了但日志没写"或者反过来 的情况不会发生。
        """
        event = TicketEvent(
            ticket_id=ticket_id,
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
        )
        db.add(event)
        if flush:
            await db.flush()
        return event

    @staticmethod
    async def list_by_ticket(
        db: AsyncSession,
        ticket_id: int,
        *,
        limit: int = 100,
    ) -> list[TicketEvent]:
        """查某张工单的操作记录，按时间正序（最早的在前面）。"""
        stmt = (
            select(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
            .order_by(TicketEvent.created_at.asc(), TicketEvent.id.asc())
            .limit(limit)
        )
        return list((await db.execute(stmt)).scalars().all())
