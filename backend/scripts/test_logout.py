"""登出黑名单测试。

★ 这个测试证明 Redis 在本项目里的真实价值：

    没有黑名单：登出只是前端删掉本地 token，
                那个 token 在过期前【依然有效】——这是 JWT 的固有缺陷。
    有了黑名单：登出后把 jti 写进 Redis，后续请求一律拒绝。

测试思路：
    ① 登录拿到 token，确认能用
    ② 调登出接口
    ③ 用【同一个 token】再请求 —— 应该 401
    ④ 检查 Redis 里确实多了一条记录
    ⑤ 验证一条没登出的 token 不受影响（不能误伤）

跑之前：
    docker compose -f docker-compose.dev.yml up -d   # 要有 Redis
    uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import argparse
import sys

import httpx

GREEN, RED, DIM, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    api = f"{args.base_url.rstrip('/')}/api/v1"

    with httpx.Client(base_url=api, timeout=20.0, trust_env=False) as c:
        # ── 0. 前置检查 ────────────────────────────────────
        print("\n[0] 前置检查")
        r = c.get("/health/redis")
        chk("Redis 可用", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        if r.status_code == 200:
            info = r.json()
            print(f"        {DIM}Redis {info.get('server_version')} / "
                  f"黑名单现有 {info.get('blacklist_size')} 条{RESET}")
            blacklist_before = info.get("blacklist_size", 0)
        else:
            print(f"{YELLOW}Redis 不可用，测试无法继续。")
            print(f"启动方式：docker compose -f docker-compose.dev.yml up -d{RESET}")
            return 1

        # ── 1. 登录两个账号 ─────────────────────────────────
        print("\n[1] 登录两个账号（一个用来登出，一个做对照）")
        r1 = c.post("/auth/login", json={"username": "admin", "password": "admin123456"})
        r2 = c.post("/auth/login", json={"username": "zhangsan", "password": "ops123456"})
        if r1.status_code != 200 or r2.status_code != 200:
            print(f"{RED}登录失败，请先跑 scripts/seed.py{RESET}")
            return 1

        token_a = r1.json()["access_token"]   # 这个待会儿要登出
        token_b = r2.json()["access_token"]   # 这个做对照，不登出
        ha = {"Authorization": f"Bearer {token_a}"}
        hb = {"Authorization": f"Bearer {token_b}"}
        print(f"        {DIM}admin 和 zhangsan 都已登录{RESET}")

        # ── 2. 登出前：两个 token 都能用 ────────────────────
        print("\n[2] 登出前，两个 token 都应该有效")
        r = c.get("/auth/me", headers=ha)
        chk("admin 的 token 可用", r.status_code == 200, str(r.status_code))
        r = c.get("/auth/me", headers=hb)
        chk("zhangsan 的 token 可用", r.status_code == 200, str(r.status_code))

        # ── 3. admin 登出 ───────────────────────────────────
        print("\n[3] admin 调用登出接口 POST /auth/logout")
        r = c.post("/auth/logout", headers=ha)
        chk("登出返回 204", r.status_code == 204, f"{r.status_code} {r.text[:200]}")

        # ── 4. ★ 关键：同一个 token 应该立即失效 ────────────
        print("\n[4] ★ 用同一个 token 再次请求（应该被拒绝）")
        r = c.get("/auth/me", headers=ha)
        chk(
            "★ 登出后原 token 立即失效（返回 401）",
            r.status_code == 401,
            f"{r.status_code} {r.text[:200]}",
        )
        if r.status_code == 401:
            body = r.json()
            chk("错误码是 UNAUTHORIZED", body.get("code") == "UNAUTHORIZED")

        # 换一个受保护接口也应该拒绝
        r = c.get("/tickets", headers=ha)
        chk(
            "★ 登出后访问其他接口同样被拒",
            r.status_code == 401,
            f"{r.status_code}",
        )

        # 重复登出应该也返回 204（幂等）
        r = c.post("/auth/logout", headers=ha)
        chk("重复登出不报错（幂等）", r.status_code in (204, 401), str(r.status_code))

        # ── 5. ★ 对照：另一个 token 不受影响 ────────────────
        print("\n[5] ★ 对照组：没登出的 token 必须不受影响")
        r = c.get("/auth/me", headers=hb)
        chk(
            "★ zhangsan 的 token 依然有效（登出不会误伤别人）",
            r.status_code == 200,
            f"{r.status_code} {r.text[:200]}",
        )

        # ── 6. Redis 里确实多了一条 ─────────────────────────
        print("\n[6] 检查 Redis 黑名单")
        r = c.get("/health/redis")
        blacklist_after = r.json().get("blacklist_size", -1)
        print(f"        {DIM}黑名单: {blacklist_before} → {blacklist_after} 条{RESET}")
        chk(
            "★ 黑名单条数增加了",
            blacklist_after > blacklist_before,
            f"{blacklist_before} → {blacklist_after}",
        )

        # ── 7. 重新登录应该能拿到新 token ───────────────────
        print("\n[7] 重新登录应该正常")
        r = c.post("/auth/login", json={"username": "admin", "password": "admin123456"})
        chk("重新登录成功", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            new_token = r.json()["access_token"]
            chk("拿到了新 token（和旧的不同）", new_token != token_a)
            r = c.get("/auth/me", headers={"Authorization": f"Bearer {new_token}"})
            chk("新 token 可用", r.status_code == 200, str(r.status_code))

    total = passed + failed
    print(f"\n{'=' * 50}")
    if failed == 0:
        print(f"{GREEN}全部通过{RESET}  {passed}/{total}")
    else:
        print(f"{RED}失败 {failed}{RESET} / 共 {total}   {GREEN}通过 {passed}{RESET}")
    print("=" * 50)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
