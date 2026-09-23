"""Dashboard 统计服务。

核心思路：**聚合交给数据库做，不要拉到 Python 里循环。**

反例（性能很差）：
    tickets = await db.execute(select(Ticket))        # 查出 10 万条
    pending = len([t for t in tickets if t.status == "pending"])   # Python 里数

正例（本文件的做法）：
    SELECT status, COUNT(*) FROM tickets GROUP BY status    # 数据库只回几行

数据库的 GROUP BY 是专门为聚合优化的，而且不需要把几万行数据
通过网络传到应用层 —— 数据量一大，差距是数量级的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cached_model, dash_key, scope_key
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User
from app.schemas.dashboard import (
    DashboardOverview,
    DistributionResponse,
    NameCount,
    SlaRiskResponse,
    TrendPoint,
    TrendResponse,
    WorkloadItem,
)
from app.schemas.ticket import TicketBrief
from app.services.sla_service import SlaService
from app.services.ticket_service import TicketService

# 中文标签：前端图表直接显示中文，不用自己维护一份映射
STATUS_LABELS = {
    TicketStatus.PENDING: "待处理",
    TicketStatus.PROCESSING: "处理中",
    TicketStatus.WAITING: "等待中",
    TicketStatus.RESOLVED: "已解决",
    TicketStatus.CLOSED: "已关闭",
}
PRIORITY_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
    "urgent": "紧急",
}
CATEGORY_LABELS = {
    "account": "账号",
    "payment": "支付",
    "technical": "技术",
    "logistics": "物流",
    "operation": "运营",
    "other": "其他",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _WorkloadWrapper(BaseModel):
    """workload 接口的缓存包装。

    cached_model 只能缓存单个 Pydantic 模型（要能 model_validate_json），
    而 workload 接口返回 list[WorkloadItem] —— 包一层才能进缓存。
    前缀下划线：纯内部实现，不出现在任何 API 契约里。
    """

    items: list[WorkloadItem] = Field(default_factory=list)


def _scoped(user: User) -> Select:
    """复用 TicketService 的数据权限逻辑。

    不重新写一遍 —— 权限规则只应该有一个定义处，
    否则哪天规则改了，Dashboard 这边的口径就和列表页对不上了。
    """
    return TicketService._apply_scope(select(Ticket), user)


class DashboardService:
    """数据看板。

    ★ 全部走 Cache-Aside 缓存（见 core/cache.py 的设计说明）：
        读 Redis → 未命中查 MySQL → 写回 Redis（带 TTL）
    缓存 key 带权限维度（all / u{用户id}），防止跨权限的缓存投毒。
    """

    @staticmethod
    async def get_overview(db: AsyncSession, user: User) -> DashboardOverview:
        """顶部概览卡片（带缓存）。"""

        async def _load() -> DashboardOverview:
            return await DashboardService._query_overview(db, user)

        return await cached_model(
            dash_key("overview", scope_key(user.role, user.id)),
            DashboardOverview,
            _load,
        )

    @staticmethod
    async def _query_overview(db: AsyncSession, user: User) -> DashboardOverview:
        """概览的真实查询（缓存未命中时才执行）。"""
        now = _utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # ── 一次查询拿到状态分布 ───────────────────────────
        # 用 case() 做条件计数，把"待处理几个、处理中几个..."
        # 压缩成一条 SQL，而不是查 5 次。
        #
        # 等价于：
        #   SELECT
        #     SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
        #     SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS processing,
        #     ...
        row = (
            await db.execute(
                _scoped(user).with_only_columns(
                    func.count(Ticket.id).label("total"),
                    func.sum(case((Ticket.status == TicketStatus.PENDING, 1), else_=0)).label("pending"),
                    func.sum(case((Ticket.status == TicketStatus.PROCESSING, 1), else_=0)).label("processing"),
                    func.sum(case((Ticket.status == TicketStatus.RESOLVED, 1), else_=0)).label("resolved"),
                    func.sum(case((Ticket.status == TicketStatus.CLOSED, 1), else_=0)).label("closed"),
                    func.sum(case((Ticket.created_at >= today_start, 1), else_=0)).label("today_created"),
                    func.sum(case((Ticket.resolved_at >= today_start, 1), else_=0)).label("today_resolved"),
                    func.sum(
                        case(
                            (
                                Ticket.sla_deadline.is_not(None)
                                & (Ticket.sla_deadline < now)
                                & Ticket.status.notin_(
                                    [TicketStatus.RESOLVED, TicketStatus.CLOSED]
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("overdue"),
                    func.sum(
                        case(
                            (
                                Ticket.assignee_id.is_(None)
                                & Ticket.status.notin_(
                                    [TicketStatus.RESOLVED, TicketStatus.CLOSED]
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("unassigned"),
                )
            )
        ).one()

        total = int(row.total or 0)
        overdue = int(row.overdue or 0)

        # "即将超时"的口径和列表页保持一致，这里用 Python 算不方便，
        # 简单用"24 小时内到期且未超时"来统计。
        at_risk_row = (
            await db.execute(
                _scoped(user)
                .with_only_columns(func.count(Ticket.id))
                .where(Ticket.sla_deadline.is_not(None))
                .where(Ticket.sla_deadline >= now)
                .where(Ticket.sla_deadline < now + timedelta(hours=24))
                .where(Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
            )
        ).scalar_one()

        return DashboardOverview(
            total=total,
            today_created=int(row.today_created or 0),
            today_resolved=int(row.today_resolved or 0),
            pending=int(row.pending or 0),
            processing=int(row.processing or 0),
            resolved=int(row.resolved or 0),
            closed=int(row.closed or 0),
            overdue=overdue,
            at_risk=int(at_risk_row or 0),
            unassigned=int(row.unassigned or 0),
        )

    @staticmethod
    async def get_trend(
        db: AsyncSession, user: User, *, days: int = 7
    ) -> TrendResponse:
        """每天的新增 / 解决数量（带缓存）。

        ★ days 参与缓存 key —— 参数不同结果不同，绝不能共享缓存。
        """

        async def _load() -> TrendResponse:
            return await DashboardService._query_trend(db, user, days=days)

        return await cached_model(
            dash_key("trend", scope_key(user.role, user.id), days),
            TrendResponse,
            _load,
        )

    @staticmethod
    async def _query_trend(
        db: AsyncSession, user: User, *, days: int = 7
    ) -> TrendResponse:
        """趋势的真实查询。

        用 GROUP BY DATE(created_at) 让数据库按天分组。
        """
        now = _utcnow()
        start = (now - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # 新增
        created_rows = (
            await db.execute(
                _scoped(user)
                .with_only_columns(
                    func.date(Ticket.created_at).label("d"),
                    func.count(Ticket.id).label("c"),
                )
                .where(Ticket.created_at >= start)
                .group_by(func.date(Ticket.created_at))
            )
        ).all()
        created_map = {str(r.d): int(r.c) for r in created_rows}

        # 解决
        resolved_rows = (
            await db.execute(
                _scoped(user)
                .with_only_columns(
                    func.date(Ticket.resolved_at).label("d"),
                    func.count(Ticket.id).label("c"),
                )
                .where(Ticket.resolved_at.is_not(None))
                .where(Ticket.resolved_at >= start)
                .group_by(func.date(Ticket.resolved_at))
            )
        ).all()
        resolved_map = {str(r.d): int(r.c) for r in resolved_rows}

        # ★ 补全没有数据的日期。
        # SQL 的 GROUP BY 只会返回"有数据的那些天"，
        # 如果 3 天前没有工单，结果里就缺这一天，
        # 前端折线图会直接把 1 号和 4 号连起来，看着像数据错了。
        # 所以要在应用层把日期补齐，缺的填 0。
        points: list[TrendPoint] = []
        for i in range(days):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            points.append(
                TrendPoint(
                    date=d,
                    created=created_map.get(d, 0),
                    resolved=resolved_map.get(d, 0),
                )
            )

        return TrendResponse(days=days, points=points)

    @staticmethod
    async def get_distribution(db: AsyncSession, user: User) -> DistributionResponse:
        """分布统计（带缓存）。"""

        async def _load() -> DistributionResponse:
            return await DashboardService._query_distribution(db, user)

        return await cached_model(
            dash_key("distribution", scope_key(user.role, user.id)),
            DistributionResponse,
            _load,
        )

    @staticmethod
    async def _query_distribution(db: AsyncSession, user: User) -> DistributionResponse:
        """分布的真实查询。"""

        async def grouped(column) -> list[NameCount]:
            rows = (
                await db.execute(
                    _scoped(user)
                    .with_only_columns(column, func.count(Ticket.id))
                    .group_by(column)
                    .order_by(func.count(Ticket.id).desc())
                )
            ).all()
            return [NameCount(name=str(k), value=int(v)) for k, v in rows]

        labels_map = {
            "status": STATUS_LABELS,
            "priority": PRIORITY_LABELS,
            "category": CATEGORY_LABELS,
        }

        async def grouped_labelled(column, kind: str) -> list[NameCount]:
            items = await grouped(column)
            mapping = labels_map[kind]
            for item in items:
                item.name = mapping.get(item.name, item.name)
            return items

        return DistributionResponse(
            by_status=await grouped_labelled(Ticket.status, "status"),
            by_priority=await grouped_labelled(Ticket.priority, "priority"),
            by_category=await grouped_labelled(Ticket.category, "category"),
        )

    @staticmethod
    async def get_sla_risk(
        db: AsyncSession, user: User, *, limit: int = 10
    ) -> SlaRiskResponse:
        """SLA 风险清单（带缓存）。

        ⚠️ 这个接口有轻微的时间敏感性（at_risk 随时间推移变化），
        缓存 60 秒意味着"剩余时间"最多滞后一分钟 —— 对风险清单的
        展示粒度来说可接受。追求精确的话应该只缓存工单列表、
        剩余时间每次现算，这里为了简单整体缓存。
        """

        async def _load() -> SlaRiskResponse:
            return await DashboardService._query_sla_risk(db, user, limit=limit)

        return await cached_model(
            dash_key("sla-risk", scope_key(user.role, user.id), limit),
            SlaRiskResponse,
            _load,
        )

    @staticmethod
    async def _query_sla_risk(
        db: AsyncSession, user: User, *, limit: int = 10
    ) -> SlaRiskResponse:
        """SLA 风险的真实查询。"""
        now = _utcnow()

        active = (
            _scoped(user)
            .where(Ticket.sla_deadline.is_not(None))
            .where(Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
        )

        total_overdue = (
            await db.execute(
                active.with_only_columns(func.count(Ticket.id)).where(
                    Ticket.sla_deadline < now
                )
            )
        ).scalar_one()

        total_at_risk = (
            await db.execute(
                active.with_only_columns(func.count(Ticket.id))
                .where(Ticket.sla_deadline >= now)
                .where(Ticket.sla_deadline < now + timedelta(hours=24))
            )
        ).scalar_one()

        # 按截止时间升序 —— 最早到期的排最前面，也就是最紧急的
        tickets = list(
            (
                await db.execute(
                    active.order_by(Ticket.sla_deadline.asc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )

        user_map, _ = await TicketService._fetch_names(db, tickets)

        items: list[TicketBrief] = []
        for t in tickets:
            brief = TicketBrief.model_validate(t)
            brief.assignee_name = user_map.get(t.assignee_id) if t.assignee_id else None
            sla_status, _ = SlaService.assess(
                created_at=t.created_at,
                sla_deadline=t.sla_deadline,
                status=t.status,
            )
            brief.sla_status = sla_status  # type: ignore[assignment]
            items.append(brief)

        return SlaRiskResponse(
            total_overdue=int(total_overdue or 0),
            total_at_risk=int(total_at_risk or 0),
            items=items,
        )

    @staticmethod
    async def get_workload(
        db: AsyncSession, user: User, *, limit: int = 10
    ) -> list[WorkloadItem]:
        """处理人工作量（带缓存）。

        ⚠️ 返回值是 list 而不是 Pydantic 模型 —— cached_model 处理不了它。
        这里手动做一层包装：包进一个临时模型再缓存，取出后拆开。
        （统一返回模型是更干净的改法，但要动接口签名 —— 不值得为缓存改 API。）
        """

        async def _load() -> _WorkloadWrapper:
            items = await DashboardService._query_workload(db, user, limit=limit)
            return _WorkloadWrapper(items=items)

        wrapper = await cached_model(
            dash_key("workload", scope_key(user.role, user.id), limit),
            _WorkloadWrapper,
            _load,
        )
        return wrapper.items

    @staticmethod
    async def _query_workload(
        db: AsyncSession, user: User, *, limit: int = 10
    ) -> list[WorkloadItem]:
        """工作量的真实查询。"""
        rows = (
            await db.execute(
                _scoped(user)
                .with_only_columns(
                    Ticket.assignee_id,
                    User.username,
                    func.sum(
                        case((Ticket.status == TicketStatus.PROCESSING, 1), else_=0)
                    ).label("processing"),
                    func.sum(
                        case((Ticket.status == TicketStatus.RESOLVED, 1), else_=0)
                    ).label("resolved"),
                    func.count(Ticket.id).label("total"),
                )
                .join(User, Ticket.assignee_id == User.id)
                .group_by(Ticket.assignee_id, User.username)
                .order_by(func.count(Ticket.id).desc())
                .limit(limit)
            )
        ).all()

        return [
            WorkloadItem(
                user_id=int(r.assignee_id),
                username=str(r.username),
                processing=int(r.processing or 0),
                resolved=int(r.resolved or 0),
                total=int(r.total or 0),
            )
            for r in rows
        ]
