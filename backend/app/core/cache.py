"""Redis 缓存：Cache-Aside 模式的通用工具。

★ 为什么 Dashboard 统计适合缓存（缓存决策的三问）：

    ① 读多写少吗？    是 —— 看板每次刷新都查，工单不是每秒都在改
    ② 计算成本高吗？  是 —— 每个统计接口是 5~9 条聚合 SQL（GROUP BY / CASE WHEN）
    ③ 允许陈旧吗？    允许 —— 统计数字晚 60 秒更新无任何业务影响

    三问都过才值得加缓存。反之（比如工单详情页）读一次就行，
    加缓存纯属复杂度浪费。

★ ★ 缓存 key 的权限隔离（本项目最重要的缓存设计）：

    数据权限因人而异：admin/operator 看全量，普通用户只看自己的。
    所以 key 必须带权限维度：

        opspilot:dash:overview:all      ← admin / operator 共享
        opspilot:dash:overview:u42      ← 普通用户每人一个

    如果不带这个维度会发生什么？—— 真实生产事故级别的坑：
        普通用户先请求 → 把"只含他自己 5 条工单"的统计写进缓存
        → admin 请求 → 命中缓存 → 看到"全公司只有 5 条工单"
    缓存把数据权限悄悄击穿了，而且很难排查（时好时坏，取决于谁先请求）。

降级策略（与 JWT 黑名单一致，见 core/redis.py）：
    Redis 不可用时直接回源查库 —— 缓存是加速器，不能变成单点故障。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

# Dashboard 缓存的 key 前缀和端点清单
DASH_PREFIX = "opspilot:dash"

# 需要参与失效的端点名（invalidate 时逐个删除）
DASH_ENDPOINTS = ("overview", "trend", "distribution", "sla-risk", "workload")


def scope_key(role: str, user_id: int) -> str:
    """把「数据权限范围」编码成 key 片段。

    admin / operator 的数据范围相同（全量），共享 "all" ——
    这既减少 key 数量，也保证两个角色之间不会互相投毒。
    普通用户每人独立 scope。
    """
    if role in ("admin", "operator"):
        return "all"
    return f"u{user_id}"


def dash_key(endpoint: str, scope: str, *params: Any) -> str:
    """拼一个 Dashboard 缓存 key。

    params 放影响结果的查询参数（如 trend 的 days）——
    参数不同结果不同，绝不能共享同一个 key。
    """
    suffix = "".join(f":{p}" for p in params if p is not None)
    return f"{DASH_PREFIX}:{endpoint}:{scope}{suffix}"


# ══════════════════════════════════════════════════════════════
# 读 / 写
# ══════════════════════════════════════════════════════════════


async def cache_get(key: str) -> str | None:
    """读缓存。未命中或 Redis 故障都返回 None（调用方回源查库）。

    fail-open：Redis 挂了 ≠ 接口挂了。缓存层必须静默降级。
    """
    try:
        client = get_redis()
        return await client.get(key)
    except Exception:  # noqa: BLE001
        logger.warning("缓存读取失败（Redis 不可用），回源查库 | key=%s", key)
        return None


async def cache_set(key: str, json_value: str, ttl: int | None = None) -> None:
    """写缓存（带 TTL）。

    ⚠️ 必须带 TTL，且 TTL 是失效策略的一半：
       即使有主动删除，也存在竞态 ——
       「请求A查库(旧数据) → 写操作删 key → 请求A把旧数据写回缓存」
       这时旧数据会驻留到 TTL 到期为止。
       所以 TTL 不是可选项，是兜底保险。
    """
    try:
        client = get_redis()
        await client.set(key, json_value, ex=ttl or settings.CACHE_DASHBOARD_TTL)
    except Exception:  # noqa: BLE001
        logger.warning("缓存写入失败（Redis 不可用）| key=%s", key)


# ══════════════════════════════════════════════════════════════
# 失效
# ══════════════════════════════════════════════════════════════


async def invalidate_dashboard(user_ids: list[int] | None = None) -> None:
    """工单数据变更后，删除受影响用户的 Dashboard 缓存。

    为什么要按「用户」删而不是全删？
        变更一张工单只影响：看到全量的人（all）+ 这张工单的创建人/负责人。
        其他用户的统计没变，删了他们只会白查一次库。

    删不干净怎么办？
        竞态窗口由 TTL 兜底（见 cache_set 的注释）。
        更彻底的方案是「版本号 key」：失效时 INCR 版本号，读时带版本读，
        旧版本 key 自然失效 —— 不用枚举删除，还根治竞态。
        本项目写路径简单，直接删够用，版本号方案记为改进方向。

    Args:
        user_ids: 这次变更波及的普通用户 id（创建人/新旧负责人等）。
                  admin/operator 共享的 "all" scope 无条件删除。
    """
    keys = [f"{DASH_PREFIX}:{ep}:all" for ep in DASH_ENDPOINTS]
    for uid in set(user_ids or []):
        keys.extend(f"{DASH_PREFIX}:{ep}:u{uid}" for ep in DASH_ENDPOINTS)

    try:
        client = get_redis()
        # 逐个 del 而不是 SCAN：key 模式是固定的 5×N 个，直接拼出来删更快，
        # 也避免 SCAN 阻塞（keys 命令在生产环境是禁用项）。
        for key in keys:
            await client.delete(key)
        logger.info("Dashboard 缓存已失效 | 波及用户=%s", user_ids or [])
    except Exception:  # noqa: BLE001
        # 删失败不阻断业务 —— 大不了统计旧 60 秒
        logger.warning("缓存失效失败（Redis 不可用）| 波及用户=%s", user_ids or [])


# ══════════════════════════════════════════════════════════════
# Cache-Aside 通用封装
# ══════════════════════════════════════════════════════════════


async def cached_model(key: str, model_cls: type, loader):
    """Cache-Aside 的模板：读缓存 → 未命中则执行 loader → 写回。

    model_cls 是 Pydantic 模型类：
        存：model.model_dump_json()
        取：model_cls.model_validate_json(raw)
    序列化交给 Pydantic，不用手拼 dict —— 字段变了存取自动跟着变。
    """
    raw = await cache_get(key)
    if raw is not None:
        try:
            return model_cls.model_validate_json(raw)
        except Exception:  # noqa: BLE001
            # 缓存里的数据结构和当前模型对不上（比如刚升级过代码）——
            # 当作未命中，回源重建。宁可多查一次库，不能报错。
            logger.warning("缓存反序列化失败，回源重建 | key=%s", key)

    data = await loader()
    await cache_set(key, data.model_dump_json())
    return data


__all__ = [
    "scope_key",
    "dash_key",
    "cache_get",
    "cache_set",
    "invalidate_dashboard",
    "cached_model",
]
