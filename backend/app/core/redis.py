"""Redis 客户端。

★ Redis 在本项目里的真实用途（不是为了用而用）：

    ① JWT 黑名单 —— 让"登出"真正生效
    ② 分布式锁   —— 保证多实例部署时定时任务只跑一次

先解释为什么需要 ①：

    JWT 是【无状态】的 —— 服务端不保存任何会话信息，只靠签名验证。
    好处是不用查库、天然支持多实例。
    代价是：**签发出去的 token 在过期前一直有效，服务端收不回来。**

    用户点了"登出"，我们只能删掉浏览器本地的 token。
    但如果这个 token 之前被人复制走了（比如 XSS 攻击、或者共享电脑上
    从 localStorage 导出来），他依然能用它访问你的系统，直到过期。

    这是 JWT 的固有缺陷，不是实现 bug。

解决方式：黑名单。

    登出时，把这个 token 的唯一标识（jti）写进 Redis，
    过期时间设成"这个 token 本来还有多久过期"。
    之后每次鉴权都先查一下黑名单，命中就拒绝。

    为什么用 Redis 而不是 MySQL？
      · 这是典型的"短期、高频读、自动过期"数据 —— Redis 的强项
      · 用 MySQL 的话要自己写清理任务删过期记录，Redis 自带 TTL
      · 查黑名单在每次请求的鉴权路径上，不能慢

    ⚠️ 代价：JWT 从"无状态"变成了"半有状态" ——
       每次请求多一次 Redis 查询。
       所以黑名单要设 TTL（token 过期后自动清理），
       而且只在必要时才用（我们只在登出时写）。

关于降级：
    Redis 挂了不能让整个系统不可用。这里的策略是
    **fail-open**：Redis 不可用时，鉴权直接放行（当作没有黑名单）。
    理由是"登出后 token 还能用一会儿"的危害，远小于"Redis 抖动导致
    所有人都登录不了"。
    这是个有意识的取舍，不同系统可以选不同的方向。
"""

from __future__ import annotations

from functools import lru_cache

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 黑名单的 key 前缀
BLACKLIST_PREFIX = "opspilot:jwt:blacklist:"


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """获取 Redis 客户端（进程内单例）。

    from_url 创建的客户端内部维护连接池，整个进程共用一个即可。
    decode_responses=True 让返回值直接是 str 而不是 bytes，
    省得到处 .decode()。
    """
    return aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        # 短超时：Redis 是加速用的，不能让它的故障拖垮整个请求。
        # 设 2 秒，挂了就快速失败走降级逻辑。
        socket_connect_timeout=2,
        socket_timeout=2,
    )


async def close_redis() -> None:
    """关闭连接池（应用退出时调用）。"""
    try:
        client = get_redis()
        await client.aclose()
        get_redis.cache_clear()
    except Exception:  # noqa: BLE001
        logger.warning("关闭 Redis 连接时出错", exc_info=True)


async def ping() -> bool:
    """探测 Redis 是否可用。健康检查用。"""
    try:
        client = get_redis()
        await client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


# ══════════════════════════════════════════════════════════════
# JWT 黑名单
# ══════════════════════════════════════════════════════════════


async def add_to_blacklist(jti: str, ttl_seconds: int) -> bool:
    """把 token 的 jti 加入黑名单。

    Args:
        jti:         token 的唯一标识（JWT 标准字段）
        ttl_seconds: 过期时间，应该等于"这个 token 还剩多久到期"。
                     设对了的话黑名单会自己清理，不需要额外的清理任务。

    Returns:
        是否成功写入（Redis 挂掉时返回 False）
    """
    if ttl_seconds <= 0:
        # token 已经过期了，不用加黑名单
        return True

    try:
        client = get_redis()
        # ex=ttl_seconds 让 Redis 自动在过期时删掉这条记录。
        #
        # ⚠️ 用 set(..., ex=) 而不是 setex()：
        # redis-py 8.x 里 setex 已标记为 deprecated，
        # 语义完全一样，但 set(ex=) 是现在的推荐写法。
        await client.set(f"{BLACKLIST_PREFIX}{jti}", "1", ex=ttl_seconds)
        logger.info("token 已加入黑名单 | jti=%s ttl=%ss", jti, ttl_seconds)
        return True
    except Exception:  # noqa: BLE001
        # ★ fail-open：Redis 挂了也不能不让用户登出。
        # 代价是"登出后 token 短期内还能用"，这个风险可以接受。
        logger.warning("写入 JWT 黑名单失败（Redis 不可用）| jti=%s", jti)
        return False


async def is_blacklisted(jti: str) -> bool:
    """检查 token 是否已被拉黑。

    ⚠️ 这个函数在【每次鉴权】时都会被调用，所以要快。
    Redis 的 GET 是 O(1)，正常情况下 1ms 以内。

    按 fail-open 策略：Redis 不可用时返回 False（视为未拉黑），
    保证登出功能故障不会导致全站无法登录。
    """
    if not jti:
        return False

    try:
        client = get_redis()
        return await client.exists(f"{BLACKLIST_PREFIX}{jti}") == 1
    except Exception:  # noqa: BLE001
        logger.warning("查询 JWT 黑名单失败（Redis 不可用），按未拉黑处理")
        return False


async def blacklist_size() -> int:
    """黑名单当前有多少条。

    这个数字可以用来看"有多少人登出过但 token 还没过期"。
    正常情况下应该很小 —— 因为每条都有 TTL，会自己消失。
    """
    try:
        client = get_redis()
        # scan_iter 而不是 keys：keys 会阻塞 Redis，生产环境禁用
        count = 0
        async for _ in client.scan_iter(match=f"{BLACKLIST_PREFIX}*", count=100):
            count += 1
        return count
    except Exception:  # noqa: BLE001
        return -1
