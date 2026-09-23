"""工单业务逻辑。

本文件是整个后端最核心的地方：
    · 状态机（哪些流转是合法的）
    · 数据权限（谁能看到哪些工单）
    · 事务边界（改工单 + 写日志必须在同一个事务里）
    · 查询组装（筛选、分页、避免 N+1）

接口层（api/v1/tickets.py）只负责转手，Agent 工具层（阶段 7）
也会调这里的同一批方法 —— 这是分层的全部意义。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.cache import invalidate_dashboard
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.core.logging import get_logger
from app.models.ticket import (
    Department,
    Ticket,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    generate_temp_ticket_no,
)
from app.models.user import User, UserRole
from app.schemas.common import PageResult
from app.schemas.ticket import (
    TicketAssign,
    TicketBrief,
    TicketCommentOut,
    TicketCreate,
    TicketDetailOut,
    TicketEventOut,
    TicketListQuery,
    TicketOut,
    TicketStats,
    TicketStatusUpdate,
    TicketUpdate,
)
from app.services.event_service import EventService
from app.services.sla_service import SlaService

logger = get_logger(__name__)


def _now() -> datetime:
    """取当前 UTC 时间（朴素时间，与数据库存储格式一致）。

    数据库里所有时间都是 UTC 的 naive datetime（不带时区信息），
    所以这里也要去掉 tzinfo 再比较，否则 Python 会抛
    "can't compare offset-naive and offset-aware datetimes"。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TicketService:
    """工单业务。"""

    # ── 状态机 ──────────────────────────────────────────────
    #
    # 这张表定义了「从某个状态出发，允许流转到哪些状态」。
    # 任何改状态的操作都必须先过这里，不允许直接赋值 ticket.status = xxx。
    #
    # 为什么要有状态机？因为状态是业务语义的载体。
    # 如果放任随便改，会出现"已关闭的工单又被改回处理中"这种
    # 破坏数据可信度的情况，而且统计报表全都会算错。
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        TicketStatus.PENDING: {TicketStatus.PROCESSING, TicketStatus.CLOSED},
        TicketStatus.PROCESSING: {
            TicketStatus.WAITING,
            TicketStatus.RESOLVED,
            TicketStatus.PENDING,  # 驳回，退回待分派池
        },
        TicketStatus.WAITING: {TicketStatus.PROCESSING, TicketStatus.CLOSED},
        TicketStatus.RESOLVED: {
            TicketStatus.CLOSED,
            TicketStatus.PROCESSING,  # 验收不通过，打回重做
        },
        TicketStatus.CLOSED: set(),  # 终态，不能再变
    }

    # 终态：到达后工单生命周期结束
    TERMINAL_STATUSES = {TicketStatus.CLOSED}

    # ══════════════════════════════════════════════════════
    # 数据权限
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _apply_scope(stmt: Select, user: User) -> Select:
        """按角色给查询加上数据范围限制。

        ⚠️ 这一步【必须】做成不可绕过的：
        不管是人工调 REST 接口，还是 Agent 调工具，
        最终都会走到这里。所以权限校验放在 Service 层，
        而不是放在接口层靠"记得加判断"。

        admin / operator  → 能看全部工单
        user              → 只能看自己创建的 或 分派给自己的
        """
        if user.role in (UserRole.ADMIN, UserRole.OPERATOR):
            return stmt

        return stmt.where(
            or_(
                Ticket.creator_id == user.id,
                Ticket.assignee_id == user.id,
            )
        )

    # ══════════════════════════════════════════════════════
    # 内部工具
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def _get_or_404(db: AsyncSession, ticket_id: int) -> Ticket:
        """按主键取工单，不存在就抛 404。"""
        ticket = await db.get(Ticket, ticket_id)
        if ticket is None:
            raise NotFoundError("工单不存在", detail={"ticket_id": ticket_id})
        return ticket

    @staticmethod
    def _check_can_modify(ticket: Ticket, user: User) -> None:
        """判断这个用户能不能改这张工单。

        规则（刻意做得简单，够用就行）：
            admin / operator          → 能改任何工单
            工单的创建人或负责人       → 能改
            其他人                     → 403
        """
        if user.role in (UserRole.ADMIN, UserRole.OPERATOR):
            return
        if ticket.creator_id == user.id or ticket.assignee_id == user.id:
            return
        raise ForbiddenError(
            "没有权限操作该工单",
            detail={"ticket_no": ticket.ticket_no},
        )

    @staticmethod
    def _build_out(
        ticket: Ticket,
        *,
        creator_name: str | None = None,
        assignee_name: str | None = None,
        department_name: str | None = None,
    ) -> TicketOut:
        """把 ORM 对象转成出参模型，并算好 SLA 状态。

        ⚠️ 注意这里用的是 TicketOut.model_validate(ticket) 之后再补字段，
        而不是直接改 ticket 对象 —— ORM 对象是数据库的映射，
        往里塞非数据库字段会污染它。
        """
        out = TicketOut.model_validate(ticket)
        out.creator_name = creator_name
        out.assignee_name = assignee_name
        out.department_name = department_name

        sla_status, remaining = SlaService.assess(
            created_at=ticket.created_at,
            sla_deadline=ticket.sla_deadline,
            status=ticket.status,
        )
        out.sla_status = sla_status  # type: ignore[assignment]
        out.sla_remaining_minutes = remaining
        return out

    @staticmethod
    async def _fetch_names(
        db: AsyncSession, tickets: list[Ticket]
    ) -> tuple[dict[int, str], dict[int, str]]:
        """批量查出用户和部门的名称。

        ★ 这是避免 N+1 的关键。

        错误做法（N+1）：
            for t in tickets:                      # 假设 20 条工单
                t.assignee_name = await get_user(t.assignee_id)   # 又查 20 次！
            总共 1 + 20 = 21 次查询。

        正确做法：
            把所有 id 收集起来，用一次 IN 查询全查回来：
                SELECT id, username FROM users WHERE id IN (1,2,3,...)
            总共 1 + 1 = 2 次查询，和工单数量无关。
        """
        user_ids = {t.creator_id for t in tickets} | {
            t.assignee_id for t in tickets if t.assignee_id
        }
        dept_ids = {t.department_id for t in tickets if t.department_id}

        user_map: dict[int, str] = {}
        if user_ids:
            rows = await db.execute(
                select(User.id, User.username).where(User.id.in_(user_ids))
            )
            user_map = dict(rows.tuples().all())  # type: ignore[arg-type]

        dept_map: dict[int, str] = {}
        if dept_ids:
            rows = await db.execute(
                select(Department.id, Department.name).where(
                    Department.id.in_(dept_ids)
                )
            )
            dept_map = dict(rows.tuples().all())  # type: ignore[arg-type]

        return user_map, dept_map

    # ══════════════════════════════════════════════════════
    # 创建
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def create(
        db: AsyncSession,
        data: TicketCreate,
        creator: User,
    ) -> TicketOut:
        """创建工单。

        流程：
            ① 校验负责人 / 部门存在（避免存进一个不存在的 id）
            ② 插入工单（此时还没有工单号）
            ③ flush 拿到自增 id，用 id 生成工单号
            ④ 按优先级算 SLA 截止时间
            ⑤ 写一条 created 操作记录
            ⑥ 一次性提交
        """
        # ── ① 校验关联对象存在 ─────────────────────────────
        if data.assignee_id is not None:
            assignee = await db.get(User, data.assignee_id)
            if assignee is None:
                raise BadRequestError(
                    "指定的负责人不存在", detail={"assignee_id": data.assignee_id}
                )

        if data.department_id is not None:
            dept = await db.get(Department, data.department_id)
            if dept is None:
                raise BadRequestError(
                    "指定的部门不存在", detail={"department_id": data.department_id}
                )

        # ── ② 插入 ─────────────────────────────────────────
        # ticket_no 先用随机值占位，第 ③ 步拿到 id 后回填真实工单号。
        # 不能用空字符串占位 —— ticket_no 有唯一索引，并发创建时会撞车。
        ticket = Ticket(
            ticket_no=generate_temp_ticket_no(),
            title=data.title,
            description=data.description,
            category=data.category,
            priority=data.priority,
            # 有负责人就是"处理中"，没有就落进"待处理"池子
            status=(
                TicketStatus.PROCESSING
                if data.assignee_id
                else TicketStatus.PENDING
            ),
            creator_id=creator.id,
            assignee_id=data.assignee_id,
            department_id=data.department_id,
        )
        db.add(ticket)

        # ── ③ flush 拿自增 id，生成工单号 ───────────────────
        #
        # flush 会把 INSERT 发给数据库，从而拿到 AUTO_INCREMENT 生成的 id，
        # 但【不提交事务】—— 后面出错还能整体回滚。
        #
        # 用 id 生成工单号的好处：天然唯一、不会并发冲突，
        # 不需要额外维护一个计数器，也不用担心"查最大值 +1"的竞态问题。
        await db.flush()
        now = _now()
        ticket.ticket_no = f"OPS-{now:%Y%m}-{ticket.id:06d}"

        # ── ④ 算 SLA 截止时间 ──────────────────────────────
        # 从创建时刻起算
        ticket.sla_deadline = await SlaService.calc_deadline(
            db, ticket.priority, base_time=now
        )

        # ── ⑤ 写操作记录 ───────────────────────────────────
        await EventService.record(
            db,
            ticket_id=ticket.id,
            user_id=creator.id,
            event_type=TicketEventType.CREATED,
            event_data={
                "ticket_no": ticket.ticket_no,
                "title": ticket.title,
                "priority": ticket.priority,
                "category": ticket.category,
                "source": "web",  # 阶段 7 接入 Agent 后会变成 "agent"
            },
        )

        # ── ⑥ 一个事务提交 ─────────────────────────────────
        # 工单和操作记录必须一起成功或一起失败，
        # 否则会出现"工单建了但没日志"的脏数据。
        await db.commit()
        await db.refresh(ticket)

        # ★ 缓存失效：工单数量变了，相关用户的 Dashboard 统计作废。
        # 在 commit 之后调用 —— 事务还没提交就删缓存，失败回滚会留下"数据没变
        # 但缓存没了"的白白多查一次（无害）；反过来才是有害的。
        # 波及：创建人 + 负责人（admin/operator 由 invalidate 内部的 all scope 覆盖）
        await invalidate_dashboard(
            [creator.id] + ([ticket.assignee_id] if ticket.assignee_id else [])
        )

        logger.info(
            "工单创建 | %s | creator=%s priority=%s",
            ticket.ticket_no,
            creator.username,
            ticket.priority,
        )

        user_map, dept_map = await TicketService._fetch_names(db, [ticket])
        return TicketService._build_out(
            ticket,
            creator_name=user_map.get(ticket.creator_id),
            assignee_name=user_map.get(ticket.assignee_id) if ticket.assignee_id else None,
            department_name=dept_map.get(ticket.department_id) if ticket.department_id else None,
        )

    # ══════════════════════════════════════════════════════
    # 查询
    # ══════════════════════════════════════════════════════

    @staticmethod
    def _build_filters(stmt: Select, query: TicketListQuery) -> Select:
        """把筛选条件拼到查询上。"""
        # in_() 对应 SQL 的 IN，一次可以筛多个值：
        #   WHERE status IN ('pending', 'processing')
        if query.status:
            stmt = stmt.where(Ticket.status.in_(query.status))
        if query.priority:
            stmt = stmt.where(Ticket.priority.in_(query.priority))
        if query.category:
            stmt = stmt.where(Ticket.category.in_(query.category))
        if query.assignee_id is not None:
            stmt = stmt.where(Ticket.assignee_id == query.assignee_id)
        if query.department_id is not None:
            stmt = stmt.where(Ticket.department_id == query.department_id)
        if query.creator_id is not None:
            stmt = stmt.where(Ticket.creator_id == query.creator_id)

        if query.keyword:
            # ilike 是大小写不敏感的 LIKE（MySQL 默认排序规则本来就不敏感）
            pattern = f"%{query.keyword}%"
            stmt = stmt.where(
                or_(Ticket.title.like(pattern), Ticket.description.like(pattern))
            )

        # ★ 场景 4 的核心：「超过 N 小时未处理」
        #
        # 两个条件缺一不可：
        #   ① created_at 早于 N 小时前
        #   ② 状态还是"待处理"（没有被分派/处理）
        #
        # 少了 ② 会把那些"早就创建、但已经处理完了"的工单也捞出来，
        # 那就是典型的业务逻辑错误。
        if query.unhandled_hours is not None:
            threshold = _now() - timedelta(hours=query.unhandled_hours)
            stmt = stmt.where(
                Ticket.created_at < threshold,
                Ticket.status == TicketStatus.PENDING,
            )

        return stmt

    @staticmethod
    async def list_tickets(
        db: AsyncSession,
        query: TicketListQuery,
        user: User,
    ) -> PageResult[TicketBrief]:
        """工单列表（带筛选、分页、数据权限）。"""
        # ── 1. 总数 ────────────────────────────────────────
        # 分页必须知道总数，否则前端画不出页码。
        # 单独查一次 count，而不是把全部数据查出来再 len()。
        count_stmt = TicketService._apply_scope(select(func.count(Ticket.id)), user)
        count_stmt = TicketService._build_filters(count_stmt, query)
        total = (await db.execute(count_stmt)).scalar_one()

        if total == 0:
            return PageResult[TicketBrief](
                items=[], total=0, page=query.page, page_size=query.page_size
            )

        # ── 2. 分页取数据 ──────────────────────────────────
        stmt = TicketService._apply_scope(select(Ticket), user)
        stmt = TicketService._build_filters(stmt, query)
        # 按创建时间倒序（最新的在前），加 id 做次级排序
        # 保证同一秒创建的工单顺序稳定（否则分页可能重复或漏数据）
        stmt = stmt.order_by(Ticket.created_at.desc(), Ticket.id.desc())
        stmt = stmt.offset((query.page - 1) * query.page_size).limit(query.page_size)

        tickets = list((await db.execute(stmt)).scalars().all())

        # ── 3. 批量补名称 ──────────────────────────────────
        user_map, dept_map = await TicketService._fetch_names(db, tickets)

        items: list[TicketBrief] = []
        for t in tickets:
            brief = TicketBrief.model_validate(t)
            brief.assignee_name = (
                user_map.get(t.assignee_id) if t.assignee_id else None
            )
            sla_status, _ = SlaService.assess(
                created_at=t.created_at,
                sla_deadline=t.sla_deadline,
                status=t.status,
            )
            brief.sla_status = sla_status  # type: ignore[assignment]
            items.append(brief)

        return PageResult[TicketBrief](
            items=items,
            total=total,
            page=query.page,
            page_size=query.page_size,
        )

    @staticmethod
    async def get_detail(db: AsyncSession, ticket_id: int, user: User) -> TicketDetailOut:
        """工单详情 = 工单 + 评论 + 操作记录。"""
        # 先按权限范围查（不能先查出来再判断权限 —— 那样
        # 通过响应时间差异可能推断出"这张工单存在但你没权限"）
        stmt = TicketService._apply_scope(select(Ticket), user).where(
            Ticket.id == ticket_id
        )
        ticket = (await db.execute(stmt)).scalars().first()
        if ticket is None:
            # 不存在和无权限返回同一个错误，
            # 避免通过错误信息探测"系统里有没有这张工单"
            raise NotFoundError("工单不存在", detail={"ticket_id": ticket_id})

        # 评论
        from app.models.ticket import TicketComment

        comment_stmt = (
            select(TicketComment)
            .where(TicketComment.ticket_id == ticket_id)
            .order_by(TicketComment.created_at.asc(), TicketComment.id.asc())
        )
        comments = list((await db.execute(comment_stmt)).scalars().all())

        # 操作记录
        events = await EventService.list_by_ticket(db, ticket_id, limit=100)

        # 批量补名称（评论人 + 操作人 + 工单相关人，一次查完）
        user_ids = {c.user_id for c in comments} | {
            e.user_id for e in events if e.user_id
        }
        user_ids |= {ticket.creator_id}
        if ticket.assignee_id:
            user_ids.add(ticket.assignee_id)

        name_map: dict[int, str] = {}
        if user_ids:
            rows = await db.execute(
                select(User.id, User.username).where(User.id.in_(user_ids))
            )
            name_map = dict(rows.tuples().all())  # type: ignore[arg-type]

        dept_name = None
        if ticket.department_id:
            dept = await db.get(Department, ticket.department_id)
            dept_name = dept.name if dept else None

        base = TicketService._build_out(
            ticket,
            creator_name=name_map.get(ticket.creator_id),
            assignee_name=name_map.get(ticket.assignee_id) if ticket.assignee_id else None,
            department_name=dept_name,
        )

        return TicketDetailOut(
            **base.model_dump(exclude={"sla_status", "sla_remaining_minutes"}),
            sla_status=base.sla_status,
            sla_remaining_minutes=base.sla_remaining_minutes,
            comments=[
                TicketCommentOut.model_validate(c).model_copy(
                    update={"author_name": name_map.get(c.user_id)}
                )
                for c in comments
            ],
            events=[
                TicketEventOut.model_validate(e).model_copy(
                    update={"actor_name": name_map.get(e.user_id) if e.user_id else "系统"}
                )
                for e in events
            ],
        )

    # ══════════════════════════════════════════════════════
    # 修改
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def update(
        db: AsyncSession,
        ticket_id: int,
        data: TicketUpdate,
        operator: User,
    ) -> TicketOut:
        """修改工单基础字段。"""
        ticket = await TicketService._get_or_404(db, ticket_id)
        TicketService._check_can_modify(ticket, operator)

        if ticket.status in TicketService.TERMINAL_STATUSES:
            raise ConflictError(
                "工单已关闭，不能再修改",
                detail={"ticket_no": ticket.ticket_no, "status": ticket.status},
            )

        # ⚠️ 这里必须同时用 exclude_unset 和 exclude_none，缺一不可。
        #
        # exclude_unset=True  只处理【真的传了】的字段（PATCH 语义）
        # exclude_none=True   再排除掉值为 None 的字段
        #
        # 为什么两个都要？因为"传了"和"传了 None"是两回事：
        #   REST 接口：前端只发 {"title": "x"} → 只改 title ✅
        #   Agent 工具：代码里写 TicketUpdate(title="x", category=None, ...)
        #              → category 被显式赋了 None，exclude_unset 认为它"传了"，
        #                于是 category=None 被写进数据库，撞 NOT NULL 约束 ❌
        #
        # 这个 bug 是 test_tools.py 抓出来的 —— 单测 REST 接口永远发现不了，
        # 因为前端不会主动发 null。这也是"同一套 Service 被两个入口调用"时
        # 才暴露的典型问题。
        #
        # 代价：无法通过这个接口把某个字段"清空成 NULL"。
        # 本项目没有这个需求（文本字段清空发空字符串即可），
        # 所以用简单可靠的方案。
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise BadRequestError("没有提供任何要修改的字段")

        old_priority = ticket.priority

        for field, value in changes.items():
            if field == "department_id" and value is not None:
                dept = await db.get(Department, value)
                if dept is None:
                    raise BadRequestError(
                        "指定的部门不存在", detail={"department_id": value}
                    )
            setattr(ticket, field, value)

        # ★ 优先级变了要重算 SLA 截止时间
        if "priority" in changes and changes["priority"] != old_priority:
            ticket.sla_deadline = await SlaService.calc_deadline(
                db,
                ticket.priority,
                base_time=ticket.created_at,  # 从创建时刻重算，而不是从现在
            )
            await EventService.record(
                db,
                ticket_id=ticket.id,
                user_id=operator.id,
                event_type=TicketEventType.PRIORITY_CHANGED,
                event_data={"from": old_priority, "to": ticket.priority},
            )

        await EventService.record(
            db,
            ticket_id=ticket.id,
            user_id=operator.id,
            event_type=TicketEventType.UPDATED,
            event_data={"fields": list(changes.keys())},
        )

        await db.commit()
        await db.refresh(ticket)

        # 改了优先级/分类会影响分布统计 → 相关用户的缓存作废
        await invalidate_dashboard(
            [operator.id, ticket.creator_id]
            + ([ticket.assignee_id] if ticket.assignee_id else [])
        )

        user_map, dept_map = await TicketService._fetch_names(db, [ticket])
        return TicketService._build_out(
            ticket,
            creator_name=user_map.get(ticket.creator_id),
            assignee_name=user_map.get(ticket.assignee_id) if ticket.assignee_id else None,
            department_name=dept_map.get(ticket.department_id) if ticket.department_id else None,
        )

    @staticmethod
    async def change_status(
        db: AsyncSession,
        ticket_id: int,
        data: TicketStatusUpdate,
        operator: User,
    ) -> TicketOut:
        """修改状态（走状态机校验）。"""
        ticket = await TicketService._get_or_404(db, ticket_id)
        TicketService._check_can_modify(ticket, operator)

        old_status = ticket.status
        new_status = data.status

        if old_status == new_status:
            raise BadRequestError(
                "工单已经是该状态", detail={"status": old_status}
            )

        # ★ 状态机校验 —— 唯一入口，不允许绕过
        allowed = TicketService.ALLOWED_TRANSITIONS.get(old_status, set())
        if new_status not in allowed:
            raise ConflictError(
                f"不能从「{old_status}」变更为「{new_status}」",
                detail={
                    "ticket_no": ticket.ticket_no,
                    "from": old_status,
                    "to": new_status,
                    "allowed": sorted(allowed),
                },
            )

        ticket.status = new_status
        now = _now()

        # 记录关键时间点，后面算"平均解决时长"要用
        if new_status == TicketStatus.RESOLVED:
            ticket.resolved_at = now
        elif new_status == TicketStatus.CLOSED:
            ticket.closed_at = now
            if ticket.resolved_at is None:
                ticket.resolved_at = now

        await EventService.record(
            db,
            ticket_id=ticket.id,
            user_id=operator.id,
            event_type=TicketEventType.STATUS_CHANGED,
            event_data={
                "from": old_status,
                "to": new_status,
                "reason": data.reason,
            },
        )

        await db.commit()
        await db.refresh(ticket)

        # 状态分布变了 → 缓存作废
        await invalidate_dashboard(
            [operator.id, ticket.creator_id]
            + ([ticket.assignee_id] if ticket.assignee_id else [])
        )

        logger.info(
            "工单状态变更 | %s | %s → %s | 操作人=%s",
            ticket.ticket_no,
            old_status,
            new_status,
            operator.username,
        )

        user_map, dept_map = await TicketService._fetch_names(db, [ticket])
        return TicketService._build_out(
            ticket,
            creator_name=user_map.get(ticket.creator_id),
            assignee_name=user_map.get(ticket.assignee_id) if ticket.assignee_id else None,
            department_name=dept_map.get(ticket.department_id) if ticket.department_id else None,
        )

    @staticmethod
    async def assign(
        db: AsyncSession,
        ticket_id: int,
        data: TicketAssign,
        operator: User,
    ) -> TicketOut:
        """分派 / 转派工单。"""
        ticket = await TicketService._get_or_404(db, ticket_id)
        TicketService._check_can_modify(ticket, operator)

        if ticket.status in TicketService.TERMINAL_STATUSES:
            raise ConflictError(
                "工单已关闭，不能分派",
                detail={"ticket_no": ticket.ticket_no},
            )

        old_assignee = ticket.assignee_id

        if data.assignee_id is not None:
            assignee = await db.get(User, data.assignee_id)
            if assignee is None:
                raise BadRequestError(
                    "指定的负责人不存在", detail={"assignee_id": data.assignee_id}
                )
            ticket.assignee_id = data.assignee_id

        if data.department_id is not None:
            dept = await db.get(Department, data.department_id)
            if dept is None:
                raise BadRequestError(
                    "指定的部门不存在", detail={"department_id": data.department_id}
                )
            ticket.department_id = data.department_id

        # 分派后自动从"待处理"进入"处理中"
        if ticket.status == TicketStatus.PENDING:
            ticket.status = TicketStatus.PROCESSING

        await EventService.record(
            db,
            ticket_id=ticket.id,
            user_id=operator.id,
            event_type=TicketEventType.ASSIGNED,
            event_data={
                "from_assignee_id": old_assignee,
                "to_assignee_id": ticket.assignee_id,
                "department_id": ticket.department_id,
            },
        )

        await db.commit()
        await db.refresh(ticket)

        # 工作量/未分派数变了 → 缓存作废。
        # 注意把【新旧两个负责人】都算上：旧负责人的工作量也变了。
        await invalidate_dashboard(
            [operator.id, ticket.creator_id, old_assignee or 0, ticket.assignee_id or 0]
        )

        user_map, dept_map = await TicketService._fetch_names(db, [ticket])
        return TicketService._build_out(
            ticket,
            creator_name=user_map.get(ticket.creator_id),
            assignee_name=user_map.get(ticket.assignee_id) if ticket.assignee_id else None,
            department_name=dept_map.get(ticket.department_id) if ticket.department_id else None,
        )

    @staticmethod
    async def delete(db: AsyncSession, ticket_id: int, operator: User) -> None:
        """删除工单。

        ⚠️ 只有 admin 能删，而且这里是【物理删除】。
        真实企业系统通常用软删除（加 is_deleted 字段），
        因为工单属于审计对象，物理删除会破坏审计链。
        这里为了演示简化，但要知道这个取舍。
        """
        if operator.role != UserRole.ADMIN:
            raise ForbiddenError("只有管理员可以删除工单")

        ticket = await TicketService._get_or_404(db, ticket_id)

        # 有关联数据时不允许删（外键约束会报错，不如提前给友好提示）
        from app.models.ticket import TicketComment

        comment_count = (
            await db.execute(
                select(func.count(TicketComment.id)).where(
                    TicketComment.ticket_id == ticket_id
                )
            )
        ).scalar_one()
        if comment_count:
            raise ConflictError(
                "该工单下还有评论，不能删除",
                detail={"ticket_no": ticket.ticket_no, "comment_count": comment_count},
            )

        ticket_no = ticket.ticket_no
        # 删除后对象就没了，先把缓存失效要用的 id 拿出来
        affected = [operator.id, ticket.creator_id] + (
            [ticket.assignee_id] if ticket.assignee_id else []
        )
        await db.delete(ticket)
        await db.commit()
        await invalidate_dashboard(affected)
        logger.warning("工单被删除 | %s | 操作人=%s", ticket_no, operator.username)

    # ══════════════════════════════════════════════════════
    # 评论
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def add_comment(
        db: AsyncSession,
        ticket_id: int,
        content: str,
        author: User,
    ) -> TicketCommentOut:
        """给工单加评论。"""
        from app.models.ticket import TicketComment

        ticket = await TicketService._get_or_404(db, ticket_id)
        TicketService._check_can_modify(ticket, author)

        comment = TicketComment(
            ticket_id=ticket_id,
            user_id=author.id,
            content=content,
        )
        db.add(comment)

        # 评论也要写一条操作记录，这样操作时间线是完整的
        await EventService.record(
            db,
            ticket_id=ticket_id,
            user_id=author.id,
            event_type=TicketEventType.COMMENTED,
            event_data={"preview": content[:50]},
        )

        await db.commit()
        await db.refresh(comment)

        return TicketCommentOut.model_validate(comment).model_copy(
            update={"author_name": author.username}
        )

    # ══════════════════════════════════════════════════════
    # 统计
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def get_statistics(
        db: AsyncSession,
        user: User,
        *,
        days: int | None = None,
    ) -> TicketStats:
        """工单统计。

        实现方式：一次查出分组的计数，而不是把数据拉到 Python 里循环。
        用 SQL 的 GROUP BY 让数据库做聚合 —— 数据库在这件事上比 Python 快得多，
        而且不用把几万行数据传到应用层。
        """
        base = TicketService._apply_scope(select(Ticket), user)
        if days is not None:
            base = base.where(Ticket.created_at >= _now() - timedelta(days=days))

        sub = base.subquery()

        async def group_count(column: str) -> dict[str, int]:
            """按某个字段分组计数。"""
            stmt = select(sub.c[column], func.count()).group_by(sub.c[column])
            rows = (await db.execute(stmt)).all()
            return {str(k): int(v) for k, v in rows}

        by_status = await group_count("status")
        by_priority = await group_count("priority")
        by_category = await group_count("category")

        total = sum(by_status.values())

        # 超时数量：状态未结束 且 sla_deadline 已过
        overdue_stmt = (
            TicketService._apply_scope(select(func.count(Ticket.id)), user)
            .where(Ticket.sla_deadline.is_not(None))
            .where(Ticket.sla_deadline < _now())
            .where(Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
        )
        if days is not None:
            overdue_stmt = overdue_stmt.where(
                Ticket.created_at >= _now() - timedelta(days=days)
            )
        overdue = (await db.execute(overdue_stmt)).scalar_one()

        unassigned_stmt = (
            TicketService._apply_scope(select(func.count(Ticket.id)), user)
            .where(Ticket.assignee_id.is_(None))
            .where(Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED]))
        )
        if days is not None:
            unassigned_stmt = unassigned_stmt.where(
                Ticket.created_at >= _now() - timedelta(days=days)
            )
        unassigned = (await db.execute(unassigned_stmt)).scalar_one()

        return TicketStats(
            total=total,
            by_status=by_status,
            by_priority=by_priority,
            by_category=by_category,
            overdue=int(overdue),
            unassigned=int(unassigned),
        )

    # ══════════════════════════════════════════════════════
    # 辅助查询（给 Agent 工具和接口层共用）
    # ══════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════
    # 批量操作（场景 4）
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def batch_update(
        db: AsyncSession,
        ticket_ids: list[int],
        *,
        priority: str | None = None,
        category: str | None = None,
        department_id: int | None = None,
        operator: User,
    ) -> dict[str, Any]:
        """批量修改工单。

        ★ 为什么"查询"和"执行"要分成两步？

        因为中间要插一个"人工确认"。Agent 先查候选集 → 生成计划 →
        等用户确认 → 再执行。所以执行这一步收到的是一份【已经确认过的
        工单 ID 列表】，而不是查询条件。

        ★ 为什么用 SAVEPOINT 而不是整体回滚？

        假设 17 条工单里有 1 条已经被别人关闭了。
          整体回滚   → 另外 16 条也白改，用户要重来一遍
          SAVEPOINT → 16 条改成功，1 条失败并说明原因

        业务上半成功的语义更合理。每条的成败都记在返回值里，
        前端可以明确展示"哪条失败了、为什么"。

        Returns:
            {"total": n, "succeeded": [...], "failed": [{ticket_id, error}...]}
        """
        succeeded: list[int] = []
        failed: list[dict[str, Any]] = []
        # 批量改动波及的所有人（缓存失效用）—— 工单被删除前先把 id 收集好
        affected_users: set[int] = {operator.id}

        for ticket_id in ticket_ids:
            try:
                # begin_nested() 创建一个 SAVEPOINT。
                # 块内出错只回滚到保存点，外层事务和已完成的行不受影响。
                async with db.begin_nested():
                    ticket = await db.get(Ticket, ticket_id)
                    if ticket is None:
                        raise NotFoundError(f"工单 {ticket_id} 不存在")

                    if ticket.status in TicketService.TERMINAL_STATUSES:
                        raise ConflictError("工单已关闭，不能修改")

                    # 收集缓存失效要波及的用户（在 SAVEPOINT 内拿，删了也有值）
                    affected_users.update({ticket.creator_id, ticket.assignee_id or 0})

                    old_priority = ticket.priority

                    if priority is not None:
                        ticket.priority = priority
                        # 改优先级要重算 SLA（和单条修改保持同一套规则）
                        ticket.sla_deadline = await SlaService.calc_deadline(
                            db, priority, base_time=ticket.created_at
                        )
                        await EventService.record(
                            db,
                            ticket_id=ticket.id,
                            user_id=operator.id,
                            event_type=TicketEventType.PRIORITY_CHANGED,
                            event_data={"from": old_priority, "to": priority, "batch": True},
                        )

                    if category is not None:
                        ticket.category = category

                    if department_id is not None:
                        old_department = ticket.department_id
                        ticket.department_id = department_id

                        # ★ 改部门也要写 assigned 事件。
                        #
                        # 这是个容易漏的点：批量改部门的入口是 batch_update
                        # 而不是 batch_assign，但业务语义上这就是一次分派。
                        # 不写这条日志的话，工单详情页的操作时间线里
                        # 会出现"部门变了但没有任何记录"的审计缺口。
                        #
                        # 审计日志的价值就在于完整性 —— 缺一条就等于没有。
                        if old_department != department_id:
                            await EventService.record(
                                db,
                                ticket_id=ticket.id,
                                user_id=operator.id,
                                event_type=TicketEventType.ASSIGNED,
                                event_data={
                                    "from_department_id": old_department,
                                    "to_department_id": department_id,
                                    "batch": True,
                                },
                            )

                        # 分派了部门就进入处理中（和单条分派规则一致）
                        if ticket.status == TicketStatus.PENDING:
                            ticket.status = TicketStatus.PROCESSING

                succeeded.append(ticket_id)

            except Exception as exc:  # noqa: BLE001
                # ★ 单条失败不影响其他条 —— 记下来继续处理下一条
                logger.info("批量修改单条失败 | ticket_id=%s | %s", ticket_id, exc)
                failed.append({"ticket_id": ticket_id, "error": str(exc)})

        # 所有行处理完后统一提交
        await db.commit()

        # 批量改动对统计影响最大（优先级分布/状态全变）→ 相关用户缓存全部作废
        await invalidate_dashboard(list(affected_users))

        logger.info(
            "批量修改完成 | 成功 %s 条 / 失败 %s 条 | 操作人=%s",
            len(succeeded),
            len(failed),
            operator.username,
        )

        return {
            "total": len(ticket_ids),
            "succeeded": succeeded,
            "succeeded_count": len(succeeded),
            "failed": failed,
            "failed_count": len(failed),
        }

    @staticmethod
    async def batch_assign(
        db: AsyncSession,
        ticket_ids: list[int],
        *,
        department_id: int | None = None,
        assignee_id: int | None = None,
        operator: User,
    ) -> dict[str, Any]:
        """批量分派工单。分批语义同 batch_update。"""
        # 先校验目标对象存在 —— 一次校验，避免在循环里重复查库
        if department_id is not None and await db.get(Department, department_id) is None:
            raise BadRequestError(
                "指定的部门不存在", detail={"department_id": department_id}
            )
        if assignee_id is not None and await db.get(User, assignee_id) is None:
            raise BadRequestError(
                "指定的负责人不存在", detail={"assignee_id": assignee_id}
            )

        succeeded: list[int] = []
        failed: list[dict[str, Any]] = []
        affected_users: set[int] = {operator.id}

        for ticket_id in ticket_ids:
            try:
                async with db.begin_nested():
                    ticket = await db.get(Ticket, ticket_id)
                    if ticket is None:
                        raise NotFoundError(f"工单 {ticket_id} 不存在")
                    if ticket.status in TicketService.TERMINAL_STATUSES:
                        raise ConflictError("工单已关闭，不能分派")

                    affected_users.update({ticket.creator_id, ticket.assignee_id or 0})

                    if assignee_id is not None:
                        ticket.assignee_id = assignee_id
                    if department_id is not None:
                        ticket.department_id = department_id
                    if ticket.status == TicketStatus.PENDING:
                        ticket.status = TicketStatus.PROCESSING

                    await EventService.record(
                        db,
                        ticket_id=ticket.id,
                        user_id=operator.id,
                        event_type=TicketEventType.ASSIGNED,
                        event_data={
                            "to_assignee_id": assignee_id,
                            "department_id": department_id,
                            "batch": True,
                        },
                    )

                succeeded.append(ticket_id)

            except Exception as exc:  # noqa: BLE001
                logger.info("批量分派单条失败 | ticket_id=%s | %s", ticket_id, exc)
                failed.append({"ticket_id": ticket_id, "error": str(exc)})

        await db.commit()

        await invalidate_dashboard(list(affected_users))

        logger.info(
            "批量分派完成 | 成功 %s 条 / 失败 %s 条 | 操作人=%s",
            len(succeeded),
            len(failed),
            operator.username,
        )

        return {
            "total": len(ticket_ids),
            "succeeded": succeeded,
            "succeeded_count": len(succeeded),
            "failed": failed,
            "failed_count": len(failed),
        }

    @staticmethod
    async def get_id_by_no(db: AsyncSession, ticket_no: str) -> int | None:
        """用工单号换主键 id。

        为什么需要这个？
        Agent 工具的入参是【工单号】（OPS-202609-000123），因为那是给人和
        模型看的标识；但数据库里所有关联都用【主键 id】。

        这个转换放在 Service 里，而不是让工具自己去写 SQL ——
        工具层一旦开始写查询，就会慢慢长成第二个数据访问层，
        分层的意义就没了。
        """
        stmt = select(Ticket.id).where(Ticket.ticket_no == ticket_no).limit(1)
        return (await db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_departments(db: AsyncSession) -> list[Department]:
        """所有部门，按 id 排序。

        场景 4 的必需能力：用户说"分配给技术部门"，Agent 拿到的是中文名称，
        必须先把名称映射成 department_id 才能调分派接口。
        """
        stmt = select(Department).order_by(Department.id)
        return list((await db.execute(stmt)).scalars().all())

    # ══════════════════════════════════════════════════════
    # 供 Agent 工具复用（阶段 7）
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def find_unhandled(
        db: AsyncSession,
        *,
        hours: int,
        user: User,
        limit: int = 100,
    ) -> list[Ticket]:
        """找出「超过 N 小时未处理」的工单。

        这是场景 4（批量升级 + 分派）的第一步。
        单独抽成一个方法，是因为 Agent 工具会直接调它拿候选集，
        拿到之后再生成计划、等人工确认。
        """
        threshold = _now() - timedelta(hours=hours)
        stmt = (
            TicketService._apply_scope(select(Ticket), user)
            .where(Ticket.created_at < threshold)
            .where(Ticket.status == TicketStatus.PENDING)
            .order_by(Ticket.created_at.asc())
            .limit(limit)
        )
        return list((await db.execute(stmt)).scalars().all())
