"""认证接口：注册、登录、获取当前用户。

承接上一讲的结论 —— 这个文件只做三件事：
    ① 声明路由  ② 声明需要什么进料  ③ 转手给 Service
一行 SQL、一条业务规则都没有。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPAuthorizationCredentials

from app.core.deps import CurrentUser, DbSession, bearer_scheme
from app.core.logging import get_logger
from app.core.redis import add_to_blacklist
from app.core.security import (
    create_access_token,
    decode_access_token,
    get_token_remaining_seconds,
)
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.schemas.common import ErrorResponse
from app.services.auth_service import AuthService

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="注册",
    responses={
        status.HTTP_409_CONFLICT: {"model": ErrorResponse, "description": "用户名或邮箱已存在"}
    },
)
async def register(data: RegisterRequest, db: DbSession) -> UserOut:
    """注册新用户。

    注意返回值类型是 UserOut 而不是 User —— Schema 层会把
    password_hash 过滤掉。这个过滤是自动的，不靠人记得。
    """
    user = await AuthService.register(db, data)
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="登录",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "用户名或密码错误"}
    },
)
async def login(data: LoginRequest, db: DbSession) -> TokenResponse:
    """校验账号密码，成功则签发 JWT。

    流程：
        前端提交用户名密码
          → AuthService 查库 + 比对密码哈希
          → 通过后签发 token
          → 前端把 token 存起来，后续请求放在 Authorization 头里
    """
    user = await AuthService.authenticate(db, data.username, data.password)
    token, expires_in = create_access_token(user_id=user.id, role=user.role)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserOut,
    summary="获取当前登录用户",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "未登录或凭证失效"}
    },
)
async def me(user: CurrentUser) -> UserOut:
    """返回当前登录用户的信息。

    这个接口本身没有业务逻辑 —— 它的价值在于【验证 token 是否有效】。
    前端刷新页面后，拿本地存的 token 调一次 /me：
        成功 → 说明 token 还能用，恢复登录态
        401  → 清掉 token，跳回登录页

    参数签名里的 `user: CurrentUser` 就是全部魔法：
    FastAPI 会自动执行 get_current_user，解析失败就抛 401，
    根本进不到这个函数体里。
    """
    return UserOut.model_validate(user)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="登出",
)
async def logout(
    user: CurrentUser,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> None:
    """登出，让当前 token 立即失效。

    ★ 这个接口不是"前端删掉本地 token 就完事"。

    如果只是前端删，服务端并不知道 —— 那个 token 在过期前依然有效。
    万一它之前被人复制走了（XSS、共享电脑导出 localStorage），
    攻击者还能继续用。

    所以这里把 token 的唯一标识（jti）写进 Redis 黑名单，
    之后每次鉴权都会先查一下，命中就拒绝。

    这个接口的存在，是为了补上 JWT"无状态"带来的固有缺口。
    """
    # 重新解析一次拿 payload。
    # 因为 get_current_user 只返回了 User 对象，没把 payload 带出来 ——
    # 想拿 jti 和 exp 就得自己再解一遍（签名已经验过了，这里不会再失败）
    token = credentials.credentials if credentials else ""
    try:
        payload = decode_access_token(token)
    except Exception:  # noqa: BLE001
        # token 已经无效了，没什么可拉黑的，直接当登出成功
        return

    jti = payload.get("jti")
    if jti:
        # TTL 设成"这个 token 本来还有多久过期"——
        # 黑名单记录会和 token 一起自然消失，不需要额外的清理任务
        await add_to_blacklist(jti, get_token_remaining_seconds(payload))

    logger.info("用户登出 | id=%s username=%s", user.id, user.username)
