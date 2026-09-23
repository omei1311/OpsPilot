"""认证服务：注册、登录。

分层回顾（对照上一讲）：
    接口层 auth.py  →  声明路由、转手
    本文件          →  真正的业务规则：查重、加密、比对
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import hash_password, verify_password
from app.models.user import User, UserRole
from app.schemas.auth import RegisterRequest

logger = get_logger(__name__)


class AuthService:
    """用户注册与登录。"""

    @staticmethod
    async def register(db: AsyncSession, data: RegisterRequest) -> User:
        """注册新用户。

        Raises:
            ConflictError: 用户名或邮箱已被占用。
        """
        # ── 1. 查重 ────────────────────────────────────────
        # or_(A, B) 就是 SQL 里的 A OR B，一条查询同时查两个字段
        stmt = select(User).where(
            or_(User.username == data.username, User.email == data.email)
        )
        existing = (await db.execute(stmt)).scalars().first()

        if existing is not None:
            # 分情况给提示，方便前端定位是哪个重复了。
            # 这里可以明确说是哪个重复了 —— 注册接口本来就能通过
            # "这个用户名被占用了吗" 来探测用户是否存在，算不上信息泄露。
            if existing.username == data.username:
                raise ConflictError("用户名已被占用", detail={"field": "username"})
            raise ConflictError("邮箱已被注册", detail={"field": "email"})

        # ── 2. 加密 ────────────────────────────────────────
        # ⚠️ bcrypt 是 CPU 密集运算，算一次要 100~300 毫秒。
        # 直接调用会把【整个事件循环】卡住这段时间，期间所有其他请求
        # 都得排队等 —— 这正是 async 最怕的"阻塞调用"。
        #
        # run_in_threadpool 把它丢到线程池里跑，事件循环就能去处理别的请求。
        # 这是异步项目里非常容易被忽略的一个坑。
        password_hash = await run_in_threadpool(hash_password, data.password)

        # ── 3. 入库 ────────────────────────────────────────
        user = User(
            username=data.username,
            email=data.email,
            password_hash=password_hash,
            role=UserRole.USER,  # 新注册的一律是普通用户，不能自己选角色
        )
        db.add(user)

        # 【关键】显式提交。回顾 db/session.py 的约定：
        # get_session 不自动 commit，事务边界由 Service 决定。
        await db.commit()
        # commit 后要 refresh 才能拿到数据库生成的 id 和 created_at
        # （因为 id 是 AUTO_INCREMENT，由 MySQL 填的，Python 这边还不知道）
        await db.refresh(user)

        logger.info("新用户注册 | id=%s username=%s", user.id, user.username)
        return user

    @staticmethod
    async def authenticate(db: AsyncSession, username: str, password: str) -> User:
        """校验用户名密码，成功返回用户对象。

        Raises:
            UnauthorizedError: 用户不存在、密码错误、或账号被禁用。
        """
        # 允许用「用户名」或「邮箱」登录
        stmt = select(User).where(
            or_(User.username == username, User.email == username)
        )
        user = (await db.execute(stmt)).scalars().first()

        # ── 安全要点：错误提示必须模糊 ──────────────────────
        # 不能写成"用户不存在" —— 那等于提供了一个接口，
        # 让攻击者能批量试出你的系统里有哪些账号。
        # 统一回"用户名或密码错误"，不透露是哪一个错了。
        if user is None:
            raise UnauthorizedError("用户名或密码错误")

        # 同样要用线程池，理由见 register()
        password_ok = await run_in_threadpool(
            verify_password, password, user.password_hash
        )
        if not password_ok:
            logger.warning("登录失败(密码错误) | username=%s", username)
            raise UnauthorizedError("用户名或密码错误")

        if not user.is_active:
            # 这个可以明确说 —— 用户确实存在且密码对了，
            # 告诉他"被禁用了"能减少无谓的客服询问。
            raise UnauthorizedError("账号已被禁用，请联系管理员")

        logger.info("用户登录成功 | id=%s username=%s", user.id, user.username)
        return user

    @staticmethod
    async def get_by_id(db: AsyncSession, user_id: int) -> User | None:
        """按主键查用户。

        db.get() 是 SQLAlchemy 专门按主键查询的快捷方法，
        比写 select(User).where(User.id == user_id) 更短，
        而且会先查 Session 的身份映射缓存，命中就不发 SQL。
        """
        return await db.get(User, user_id)
