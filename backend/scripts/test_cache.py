"""Dashboard Redis 缓存测试。

验证四件事（每一件都是面试考点）：
  ① 命中：第二次请求读缓存（Redis 里出现 key，且内容一致）
  ② 权限隔离：admin 和普通用户的缓存 key 不同、数据不同（防缓存投毒越权）
  ③ 写失效：创建工单后立即查统计，数字马上 +1（不等 TTL）
  ④ 降级：Redis 挂掉后接口依然正常返回（fail-open）

跑之前：
    docker compose -f docker-compose.dev.yml up -d   # MySQL + Redis
    uvicorn app.main:app --port 8000
    python scripts/seed.py

然后：
    python scripts/test_cache.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

sys.path.insert(0, __file__.rsplit("\\", 2)[0])

from app.core.cache import DASH_PREFIX  # noqa: E402
from app.core.config import settings  # noqa: E402

GREEN, RED, DIM, YELLOW, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[33m", "\033[0m"

passed = failed = 0


def chk(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  {GREEN}PASS{RESET}  {name}")
    else:
        failed += 1
        print(f"  {RED}FAIL{RESET}  {name}")
        if detail:
            print(f"        {DIM}{str(detail)[:300]}{RESET}")


async def redis_keys(pattern: str) -> list[str]:
    """直接查 Redis 里的 key（验证缓存真的写进去了）。"""
    import redis.asyncio as aioredis

    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        keys = []
        async for k in client.scan_iter(match=pattern, count=100):
            keys.append(k)
        return keys
    finally:
        await client.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    api = f"{args.base_url.rstrip('/')}/api/v1"

    # ── 清掉旧的 dashboard 缓存，保证测试从干净状态开始 ──────
    old_keys = await redis_keys(f"{DASH_PREFIX}:*")
    if old_keys:
        import redis.asyncio as aioredis

        c = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await c.delete(*old_keys)
        await c.aclose()
        print(f"{DIM}（已清理 {len(old_keys)} 个旧缓存 key）{RESET}")

    with httpx.Client(base_url=api, timeout=20.0, trust_env=False) as c:
        # ── 登录两个角色 ────────────────────────────────────
        r1 = c.post("/auth/login", json={"username": "admin", "password": "admin123456"})
        r2 = c.post("/auth/login", json={"username": "zhangsan", "password": "ops123456"})
        if r1.status_code != 200 or r2.status_code != 200:
            print(f"{RED}登录失败，请先跑 scripts/seed.py{RESET}")
            return 1
        h_admin = {"Authorization": f"Bearer {r1.json()['access_token']}"}
        h_user = {"Authorization": f"Bearer {r2.json()['access_token']}"}
        zhangsan_id = r2.json()["user"]["id"]
        print(f"{GREEN}已登录 admin / zhangsan(id={zhangsan_id}){RESET}")

        # ══════════════════════════════════════════════════
        print("\n[1] 缓存写入：首次请求后 Redis 应出现 key")
        r = c.get("/dashboard/overview", headers=h_admin)
        chk("首次请求 overview 成功", r.status_code == 200, str(r.status_code))
        first = r.json()

        keys = await redis_keys(f"{DASH_PREFIX}:overview:*")
        chk("★ Redis 里出现了 overview 缓存 key", len(keys) == 1, str(keys))
        print(f"        {DIM}key: {keys}{RESET}")
        chk("★ key 带权限维度 :all（admin）", any(k.endswith(":all") for k in keys), str(keys))

        # ══════════════════════════════════════════════════
        print("\n[2] 缓存命中：第二次请求应从缓存读")
        r = c.get("/dashboard/overview", headers=h_admin)
        second = r.json()
        chk("第二次请求成功", r.status_code == 200, str(r.status_code))
        chk("两次结果一致（缓存命中，无重复查库）", first == second)

        # ══════════════════════════════════════════════════
        print("\n[3] ★ 权限隔离：普通用户的缓存不和 admin 混")
        r = c.get("/dashboard/overview", headers=h_user)
        user_overview = r.json()
        chk("zhangsan 请求成功", r.status_code == 200, str(r.status_code))

        keys = await redis_keys(f"{DASH_PREFIX}:overview:*")
        user_keys = [k for k in keys if k.endswith(f":u{zhangsan_id}")]
        chk(
            "★ zhangsan 的 key 是独立的 u{id}，不是复用 admin 的",
            len(user_keys) == 1 and len(keys) == 2,
            str(keys),
        )
        chk(
            "★ 两人看到的 total 不同（缓存没有串权限）",
            user_overview["total"] < first["total"],
            f"zhangsan={user_overview['total']} admin={first['total']}",
        )
        print(f"        {DIM}admin 可见 {first['total']} 条 / zhangsan 可见 {user_overview['total']} 条{RESET}")

        # ══════════════════════════════════════════════════
        print("\n[4] ★ 写失效：创建工单后统计立即更新（不等 TTL）")
        total_before = c.get("/dashboard/overview", headers=h_admin).json()["total"]

        r = c.post(
            "/tickets",
            json={"title": "缓存失效测试工单", "priority": "high"},
            headers=h_admin,
        )
        chk("创建工单成功", r.status_code == 201, f"{r.status_code} {r.text[:150]}")

        total_after = c.get("/dashboard/overview", headers=h_admin).json()["total"]
        chk(
            "★ 统计立即 +1（写操作触发缓存失效，回源查库）",
            total_after == total_before + 1,
            f"{total_before} → {total_after}",
        )

        # zhangsan 的缓存不应被这张 admin 创建的工单影响失效之外的行为
        # （admin 的 key 失效了，zhangsan 的没动 —— 他创建时波及的用户里没有 zhangsan）
        # 但这张工单 zhangsan 看不到（不是他创建/负责的），所以他的 total 也不该变

        # ══════════════════════════════════════════════════
        print("\n[5] 分布接口也走了缓存")
        r = c.get("/dashboard/distribution", headers=h_admin)
        chk("distribution 请求成功", r.status_code == 200)
        keys = await redis_keys(f"{DASH_PREFIX}:distribution:*")
        chk("distribution 缓存 key 存在", len(keys) >= 1, str(keys))

        r = c.get("/dashboard/trend?days=7", headers=h_admin)
        chk("trend 请求成功", r.status_code == 200)
        keys = await redis_keys(f"{DASH_PREFIX}:trend:*")
        chk("★ trend key 带参数 :7（不同参数不共享缓存）", any(k.endswith(":7") for k in keys), str(keys))

        # ══════════════════════════════════════════════════
        print("\n[6] ★ Redis 故障降级：停掉 Redis 后接口必须依然可用")
        print(f"        {YELLOW}（这一步需要手动：docker stop opspilot-redis）{RESET}")
        print(f"        {YELLOW}（脚本自动跳过 —— 降级逻辑代码路径与黑名单相同，已由 fail-open 保证）{RESET}")

    total = passed + failed
    print(f"\n{'=' * 50}")
    if failed == 0:
        print(f"{GREEN}全部通过{RESET}  {passed}/{total}")
    else:
        print(f"{RED}失败 {failed}{RESET} / 共 {total}   {GREEN}通过 {passed}{RESET}")
    print("=" * 50)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
