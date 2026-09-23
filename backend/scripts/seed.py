"""演示数据种子脚本。

用法::

    cd backend
    .venv\\Scripts\\python.exe scripts/seed.py           # 追加数据
    .venv\\Scripts\\python.exe scripts/seed.py --reset   # 先清空业务数据再生成

⚠️ --reset 会删掉所有工单/评论/记录，但【不会】删用户（避免把你自己注册的账号删了）。

生成的数据刻意做了时间分布：有 20 条「超过 24 小时未处理」的待处理工单，
这是场景 4（批量升级 + 分派）演示的前提。没有这批数据，那个 Demo 就没东西可改。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import random
import sys
from datetime import datetime, timedelta, timezone

# ⚠️ 这两行必须在 import app 之前执行。
#
# 直接 `python scripts/seed.py` 时，Python 会把【脚本所在目录】(scripts/)
# 放进模块搜索路径，而不是 backend/。所以 `from app.xxx import ...` 会报
# ModuleNotFoundError: No module named 'app'。
#
# 把 backend/ 手动插到搜索路径最前面，问题就解决了。
# 这也是为什么 uvicorn / pytest / alembic 都需要额外配置 —— 它们各自的
# 机制不同，但本质都是"要告诉 Python 去哪找 app 包"。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.session import AsyncSessionLocal, dispose_engine  # noqa: E402
from app.models.sla import SlaPolicy  # noqa: E402
from app.models.ticket import (  # noqa: E402
    Department,
    Ticket,
    TicketCategory,
    TicketComment,
    TicketEvent,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    generate_temp_ticket_no,
)
from app.models.user import User, UserRole  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════════
# 基础数据
# ══════════════════════════════════════════════════════════════

DEPARTMENTS = [
    ("技术部", "tech"),
    ("客服部", "cs"),
    ("运维部", "ops"),
    ("产品部", "product"),
]

SLA_POLICIES = [
    # (优先级, 响应分钟, 解决分钟)
    (TicketPriority.URGENT, 15, 240),
    (TicketPriority.HIGH, 60, 480),
    (TicketPriority.MEDIUM, 240, 1440),
    (TicketPriority.LOW, 480, 2880),
]

USERS = [
    # (用户名, 邮箱, 密码, 角色)
    ("admin", "admin@opspilot.local", "admin123456", UserRole.ADMIN),
    ("operator", "operator@opspilot.local", "ops123456", UserRole.OPERATOR),
    ("zhangsan", "zhangsan@opspilot.local", "ops123456", UserRole.USER),
    ("lisi", "lisi@opspilot.local", "ops123456", UserRole.USER),
    ("wangwu", "wangwu@opspilot.local", "ops123456", UserRole.USER),
]

# 工单模板：(标题, 描述, 分类)
TICKET_TEMPLATES = [
    ("支付接口大量返回 502", "线上支付接口从今天下午开始大量返回 502，用户无法完成支付。", TicketCategory.PAYMENT),
    ("订单同步失败", "订单系统与仓储系统数据不一致，部分订单状态未同步。", TicketCategory.TECHNICAL),
    ("用户无法登录", "多个用户反馈输入正确密码后仍提示登录失败。", TicketCategory.ACCOUNT),
    ("报表导出超时", "数据报表导出功能在大数据量下超时失败。", TicketCategory.TECHNICAL),
    ("物流单号查询异常", "物流单号查询页面报错，无法显示物流轨迹。", TicketCategory.LOGISTICS),
    ("优惠券无法使用", "用户反馈优惠券结算时提示不可用。", TicketCategory.OPERATION),
    ("账号被误锁定", "用户账号因多次输错密码被锁定，请求解锁。", TicketCategory.ACCOUNT),
    ("移动端页面白屏", "部分安卓机型打开订单页出现白屏。", TicketCategory.TECHNICAL),
    ("退款到账延迟", "用户申请退款后超过 7 天仍未到账。", TicketCategory.PAYMENT),
    ("API 调用频率受限", "第三方对接方反馈调用接口频繁被限流。", TicketCategory.TECHNICAL),
    ("数据看板数字不准确", "运营看板显示的日活与后台统计不一致。", TicketCategory.OPERATION),
    ("短信验证码收不到", "部分用户反馈收不到短信验证码。", TicketCategory.ACCOUNT),
    ("批量导入失败", "商品批量导入功能提示格式错误但文件正常。", TicketCategory.OPERATION),
    ("服务器磁盘告警", "监控显示应用服务器磁盘使用率超过 90%。", TicketCategory.TECHNICAL),
    ("支付回调丢失", "部分订单支付成功但系统未收到回调。", TicketCategory.PAYMENT),
]


# ══════════════════════════════════════════════════════════════
# 生成逻辑
# ══════════════════════════════════════════════════════════════


async def seed(reset: bool) -> None:
    async with AsyncSessionLocal() as db:
        # ── 可选：清空业务数据 ─────────────────────────────
        if reset:
            print("清空已有业务数据...")
            # 有外键依赖，删除要按依赖顺序倒着来
            await db.execute(delete(TicketEvent))
            await db.execute(delete(TicketComment))
            await db.execute(delete(Ticket))
            await db.commit()

        # ── 部门 ───────────────────────────────────────────
        existing_depts = {
            d.code: d for d in (await db.execute(select(Department))).scalars().all()
        }
        for name, code in DEPARTMENTS:
            if code not in existing_depts:
                db.add(Department(name=name, code=code))
        await db.commit()

        depts = {
            d.code: d for d in (await db.execute(select(Department))).scalars().all()
        }
        print(f"部门: {len(depts)} 个")

        # ── SLA 策略 ───────────────────────────────────────
        existing_sla = {
            p.priority: p for p in (await db.execute(select(SlaPolicy))).scalars().all()
        }
        for priority, resp, reso in SLA_POLICIES:
            if priority not in existing_sla:
                db.add(
                    SlaPolicy(
                        priority=priority,
                        response_minutes=resp,
                        resolution_minutes=reso,
                    )
                )
        await db.commit()
        print(f"SLA 策略: {len(SLA_POLICIES)} 条")

        # ── 用户 ───────────────────────────────────────────
        existing_users = {
            u.username: u for u in (await db.execute(select(User))).scalars().all()
        }
        created_users = 0
        for username, email, password, role in USERS:
            if username not in existing_users:
                db.add(
                    User(
                        username=username,
                        email=email,
                        password_hash=hash_password(password),
                        role=role,
                    )
                )
                created_users += 1
        await db.commit()

        users = {
            u.username: u for u in (await db.execute(select(User))).scalars().all()
        }
        print(f"用户: 新建 {created_users} 个，共 {len(users)} 个")

        # ── 工单 ───────────────────────────────────────────
        existing_count = (
            await db.execute(select(func.count(Ticket.id)))
        ).scalar_one()

        admin = users.get("admin")
        operator = users.get("operator")
        engineers = [users[n] for n in ("zhangsan", "lisi", "wangwu") if n in users]
        creators = [u for u in (admin, operator, *engineers) if u]

        if not creators:
            print("没有可用用户，跳过工单生成")
            return

        now = _now()
        tickets: list[Ticket] = []

        # ── A. 20 条「超过 24 小时未处理的待处理工单」────────
        # 这是场景 4 的演示素材，必须保证数量足够
        for i in range(20):
            title, desc, cat = TICKET_TEMPLATES[i % len(TICKET_TEMPLATES)]
            hours_ago = random.randint(25, 96)  # 25~96 小时前创建
            created = now - timedelta(hours=hours_ago)
            priority = random.choice(
                [TicketPriority.MEDIUM, TicketPriority.HIGH, TicketPriority.LOW]
            )
            tickets.append(
                Ticket(
                    ticket_no=generate_temp_ticket_no(),
                    title=f"{title}（第 {i + 1} 例）",
                    description=desc,
                    category=cat,
                    priority=priority,
                    status=TicketStatus.PENDING,  # ★ 关键：还是待处理
                    creator_id=random.choice(creators).id,
                    assignee_id=None,  # ★ 关键：没有负责人
                    department_id=None,
                    created_at=created,
                    updated_at=created,
                    sla_deadline=created + timedelta(hours=24),
                )
            )

        # ── B. 15 条已分派、处理中的工单 ────────────────────
        for i in range(15):
            title, desc, cat = TICKET_TEMPLATES[i % len(TICKET_TEMPLATES)]
            hours_ago = random.randint(2, 48)
            created = now - timedelta(hours=hours_ago)
            engineer = random.choice(engineers) if engineers else random.choice(creators)
            priority = random.choice(list(TicketPriority))
            tickets.append(
                Ticket(
                    ticket_no=generate_temp_ticket_no(),
                    title=f"{title}（处理中 {i + 1}）",
                    description=desc,
                    category=cat,
                    priority=priority,
                    status=TicketStatus.PROCESSING,
                    creator_id=random.choice(creators).id,
                    assignee_id=engineer.id,
                    department_id=depts["tech"].id if "tech" in depts else None,
                    created_at=created,
                    updated_at=created,
                    sla_deadline=created + timedelta(hours=8),
                )
            )

        # ── C. 10 条已解决 ─────────────────────────────────
        #
        # 时间刻意做得【宽松】，让它们都不超时。
        # 如果这批也超时，"已解决"的工单在统计里仍显示超时，看着很怪。
        for i in range(10):
            title, desc, cat = TICKET_TEMPLATES[i % len(TICKET_TEMPLATES)]
            created = now - timedelta(hours=random.randint(30, 120))
            resolved = created + timedelta(hours=random.randint(1, 5))
            tickets.append(
                Ticket(
                    ticket_no=generate_temp_ticket_no(),
                    title=f"{title}（已解决 {i + 1}）",
                    description=desc,
                    category=cat,
                    priority=random.choice(list(TicketPriority)),
                    status=TicketStatus.RESOLVED,
                    creator_id=random.choice(creators).id,
                    assignee_id=random.choice(engineers).id if engineers else None,
                    department_id=depts["tech"].id if "tech" in depts else None,
                    created_at=created,
                    updated_at=resolved,
                    resolved_at=resolved,
                    # 截止时间设在解决时间之后，保证不超时
                    sla_deadline=resolved + timedelta(hours=2),
                )
            )

        # ── D. 8 条已关闭 ──────────────────────────────────
        for i in range(8):
            title, desc, cat = TICKET_TEMPLATES[i % len(TICKET_TEMPLATES)]
            created = now - timedelta(hours=random.randint(48, 200))
            closed = created + timedelta(hours=random.randint(2, 10))
            tickets.append(
                Ticket(
                    ticket_no=generate_temp_ticket_no(),
                    title=f"{title}（已关闭 {i + 1}）",
                    description=desc,
                    category=cat,
                    priority=random.choice(list(TicketPriority)),
                    status=TicketStatus.CLOSED,
                    creator_id=random.choice(creators).id,
                    assignee_id=random.choice(engineers).id if engineers else None,
                    department_id=depts["cs"].id if "cs" in depts else None,
                    created_at=created,
                    updated_at=closed,
                    resolved_at=closed,
                    closed_at=closed,
                    sla_deadline=closed + timedelta(hours=2),
                )
            )

        # ── E. 7 条 SLA 已超时或即将超时（用于场景 3）────────
        for i in range(7):
            title, desc, cat = TICKET_TEMPLATES[(i + 3) % len(TICKET_TEMPLATES)]
            hours_ago = random.randint(6, 40)
            created = now - timedelta(hours=hours_ago)
            priority = random.choice([TicketPriority.URGENT, TicketPriority.HIGH])
            # 截止时间已过 → 状态是 overdue
            deadline = now - timedelta(hours=random.randint(1, 20))
            tickets.append(
                Ticket(
                    ticket_no=generate_temp_ticket_no(),
                    title=f"{title}（SLA 风险 {i + 1}）",
                    description=desc,
                    category=cat,
                    priority=priority,
                    status=random.choice(
                        [TicketStatus.PENDING, TicketStatus.PROCESSING]
                    ),
                    creator_id=random.choice(creators).id,
                    assignee_id=random.choice(engineers).id if engineers else None,
                    department_id=depts["ops"].id if "ops" in depts else None,
                    created_at=created,
                    updated_at=created,
                    sla_deadline=deadline,
                )
            )

        db.add_all(tickets)

        # ── 生成工单号 ─────────────────────────────────────
        #
        # 用「已有最大 id + 序号」的方式，避免和已有工单号冲突。
        # flush 之后每张工单就拿到了自增 id。
        await db.flush()
        for t in tickets:
            t.ticket_no = f"OPS-{t.created_at:%Y%m}-{t.id:06d}"

        # ── 操作记录 ───────────────────────────────────────
        events: list[TicketEvent] = []
        for t in tickets:
            events.append(
                TicketEvent(
                    ticket_id=t.id,
                    user_id=t.creator_id,
                    event_type=TicketEventType.CREATED,
                    event_data={"ticket_no": t.ticket_no, "source": "seed"},
                    created_at=t.created_at,
                    updated_at=t.created_at,
                )
            )
            if t.assignee_id:
                events.append(
                    TicketEvent(
                        ticket_id=t.id,
                        user_id=admin.id if admin else t.creator_id,
                        event_type=TicketEventType.ASSIGNED,
                        event_data={"to_assignee_id": t.assignee_id},
                        created_at=t.created_at + timedelta(minutes=30),
                        updated_at=t.created_at + timedelta(minutes=30),
                    )
                )
            if t.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
                events.append(
                    TicketEvent(
                        ticket_id=t.id,
                        user_id=t.assignee_id or t.creator_id,
                        event_type=TicketEventType.STATUS_CHANGED,
                        event_data={"from": "processing", "to": t.status},
                        created_at=t.updated_at,
                        updated_at=t.updated_at,
                    )
                )
        db.add_all(events)

        # ── 评论 ───────────────────────────────────────────
        comments: list[TicketComment] = []
        COMMENT_TEXTS = [
            "已收到，正在排查。",
            "麻烦提供一下具体的订单号，方便定位。",
            "初步判断是上游服务超时导致的。",
            "已联系第三方服务商确认。",
            "问题已定位，正在修复中。",
            "已修复并验证，请确认。",
        ]
        for t in tickets:
            # 一半工单有 1~3 条评论
            for _ in range(random.randint(0, 3)):
                author = random.choice(creators).id
                offset = timedelta(minutes=random.randint(10, 300))
                comments.append(
                    TicketComment(
                        ticket_id=t.id,
                        user_id=author,
                        content=random.choice(COMMENT_TEXTS),
                        created_at=t.created_at + offset,
                        updated_at=t.created_at + offset,
                    )
                )
        db.add_all(comments)

        await db.commit()

        # ── 汇总 ───────────────────────────────────────────
        total_tickets = (await db.execute(select(func.count(Ticket.id)))).scalar_one()
        pending_unhandled = (
            await db.execute(
                select(func.count(Ticket.id))
                .where(Ticket.status == TicketStatus.PENDING)
                .where(Ticket.created_at < now - timedelta(hours=24))
            )
        ).scalar_one()

        print()
        print("=" * 50)
        print(f"本次新建工单: {len(tickets)} 条（库中总计 {total_tickets} 条）")
        print(f"  其中「超过 24 小时未处理」: {pending_unhandled} 条  ← 场景 4 的素材")
        print(f"操作记录: {len(events)} 条")
        print(f"评论: {len(comments)} 条")
        print("=" * 50)
        print()
        print("演示账号（密码见括号）:")
        print("  admin    / admin123456  管理员")
        print("  operator / ops123456    操作员")
        print("  zhangsan / ops123456    普通用户")
        print()


async def _run(reset: bool) -> None:
    """在一个事件循环里跑完生成 + 释放连接。

    ⚠️ 不要把 seed() 和 dispose_engine() 分成两次 asyncio.run()。
    连接池里的连接是绑定在【创建它的那个事件循环】上的，
    第一个 asyncio.run() 结束时循环就关了，第二个循环再去用那些连接
    会报 "'NoneType' object has no attribute 'send'"。
    必须在同一个循环里完成。
    """
    try:
        await seed(reset=reset)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 OpsPilot 演示数据")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先清空所有工单/评论/记录再生成（不删用户）",
    )
    args = parser.parse_args()
    asyncio.run(_run(reset=args.reset))


if __name__ == "__main__":
    main()
