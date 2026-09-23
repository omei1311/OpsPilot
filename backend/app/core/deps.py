"""FastAPI 依赖注入定义。

所有可复用的依赖都集中在这里，Router 只 import 类型别名，不自己拼 Depends。

用 Annotated 而不是默认值写法的好处::

    # ❌ 旧写法：类型和依赖混在一起，同一个依赖要重复写很多遍
    async def list_tickets(db: AsyncSession = Depends(get_session)): ...

    # ✅ Annotated：把「依赖」这件事收敛成一个可复用的类型别名
    DbSession = Annotated[AsyncSession, Depends(get_session)]

    async def list_tickets(db: DbSession): ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.redis import is_blacklisted
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.user import User

# ══════════════════════════════════════════════════════════════
# 数据库会话
# ══════════════════════════════════════════════════════════════

# 请求级数据库会话。语义：一次 HTTP 请求 = 一个 Session = 一个事务边界。
DbSession = Annotated[AsyncSession, Depends(get_session)]


# ══════════════════════════════════════════════════════════════
# 认证
# ══════════════════════════════════════════════════════════════

# HTTPBearer 负责从请求头里取出：
#     Authorization: Bearer eyJhbGciOi...
#                            └────── 这一段就是 token
#
# auto_error=False 的意思：没带 token 时【不要】自动报错，
# 而是把 None 交给我们的函数处理 —— 这样能返回我们自己定义的
# 错误格式，而不是 FastAPI 默认的 {"detail": "Not authenticated"}。
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    db: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> User:
    """从请求头解析 JWT，查出当前登录用户。

    这是"受保护接口"的守门人。任何需要登录的接口只要声明
    `user: CurrentUser`，FastAPI 就会先执行这个函数。

    Raises:
        UnauthorizedError: 没带 token / token 无效 / 用户不存在或被禁用。
    """
    # ── 1. 有没有带 token ──────────────────────────────────
    if credentials is None:
        raise UnauthorizedError("请先登录")

    # ── 2. token 本身是否有效 ──────────────────────────────
    # decode_access_token 内部会校验签名和过期时间，失败会抛 UnauthorizedError
    payload = decode_access_token(credentials.credentials)

    # ── 3. 是否已被拉黑（用户登出过）────────────────────────
    #
    # ★ 这一步是让"登出"真正生效的关键。
    #
    # 没有它的话：用户点了登出，我们只是删掉浏览器本地的 token。
    # 如果这个 token 之前被复制走了（XSS、共享电脑导出 localStorage），
    # 攻击者在 token 过期前依然能访问系统 —— 这是 JWT 的固有缺陷。
    #
    # 有它之后：登出时把 jti 写进 Redis 黑名单，这里拦下来。
    #
    # 代价：每次鉴权多一次 Redis 查询（约 0.5ms）。
    # Redis 挂掉时按 fail-open 处理（当作没拉黑），
    # 理由见 core/redis.py —— 登出功能故障不该导致全站登录不了。
    jti = payload.get("jti")
    if jti and await is_blacklisted(jti):
        raise UnauthorizedError("登录已失效，请重新登录")

    # ── 4. 用户是否还真实存在 ──────────────────────────────
    # 这一步不能省。token 里的信息是【签发那一刻】的快照，
    # 签发之后用户可能已经被删了、被禁用了。
    # 只验签名不查库，等于给已注销用户留了一把永久钥匙。
    user_id = int(payload["sub"])
    user = await db.get(User, user_id)

    if user is None:
        raise UnauthorizedError("用户不存在")
    if not user.is_active:
        raise UnauthorizedError("账号已被禁用")

    return user


# 用法：在接口签名里写 `user: CurrentUser` 即可
CurrentUser = Annotated[User, Depends(get_current_user)]


# ══════════════════════════════════════════════════════════════
# 权限守卫
# ══════════════════════════════════════════════════════════════


def require_roles(*roles: str):
    """角色守卫工厂：只有指定角色能访问。

    用法::

        @router.delete("/users/{id}")
        async def delete_user(user: Annotated[User, Depends(require_roles("admin"))]):
            ...

    401 和 403 的区别（面试常考）：
        401 = 你是谁我不知道      → 应该去登录
        403 = 我知道你是谁，但你没权限 → 登录了也没用，别试了
    """

    async def _checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise ForbiddenError(
                "没有权限执行该操作",
                detail={"required_roles": list(roles), "your_role": user.role},
            )
        return user

    return _checker


__all__ = [
    "DbSession",
    "CurrentUser",
    "bearer_scheme",
    "get_session",
    "get_current_user",
    "require_roles",
]
