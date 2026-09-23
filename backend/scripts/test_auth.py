"""认证流程端到端测试。

跑之前先启动服务：
    uvicorn app.main:app --port 8000

然后：
    python scripts/test_auth.py

覆盖：注册 → 重复注册 → 登录 → 密码错误 → 带 token 取用户 → 无 token → 伪造 token
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys

import httpx

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  {GREEN}PASS{RESET}  {name}")
    else:
        failed += 1
        print(f"  {RED}FAIL{RESET}  {name}")
        if detail:
            print(f"        {DIM}{detail}{RESET}")


def show(label: str, data) -> None:
    print(f"        {DIM}{label}: {json.dumps(data, ensure_ascii=False)}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1"

    # 每次跑用随机用户名，避免和上次的数据撞车
    suffix = "".join(random.choices(string.digits, k=6))
    username = f"testuser{suffix}"
    email = f"test{suffix}@example.com"
    password = "ops123456"

    with httpx.Client(base_url=api, timeout=15.0, trust_env=False) as c:
        # ── 1. 注册 ────────────────────────────────────────
        print("\n[1] 注册 POST /auth/register")
        r = c.post("/auth/register", json={
            "username": username, "email": email, "password": password,
        })
        check("返回 201", r.status_code == 201, f"实际 {r.status_code} {r.text[:200]}")
        if r.status_code == 201:
            body = r.json()
            show("响应", body)
            check("返回了 id", isinstance(body.get("id"), int))
            check("role 默认是 user", body.get("role") == "user")
            check(
                "★ 响应里没有 password_hash（Schema 过滤生效）",
                "password_hash" not in body,
                json.dumps(body, ensure_ascii=False),
            )
            check("响应里没有 password", "password" not in body)

        # ── 2. 重复注册 ────────────────────────────────────
        print("\n[2] 重复注册（应该被拦住）")
        r = c.post("/auth/register", json={
            "username": username, "email": f"other{suffix}@example.com",
            "password": password,
        })
        check("返回 409", r.status_code == 409, f"实际 {r.status_code}")
        if r.status_code == 409:
            body = r.json()
            show("响应", body)
            check("code == CONFLICT", body.get("code") == "CONFLICT")
            check("指出是 username 重复", body.get("detail", {}).get("field") == "username")

        # ── 3. 邮箱格式校验 ────────────────────────────────
        print("\n[3] 邮箱格式非法（Pydantic 应该拦住）")
        r = c.post("/auth/register", json={
            "username": f"bad{suffix}", "email": "这不是邮箱", "password": password,
        })
        check("返回 422", r.status_code == 422, f"实际 {r.status_code}")
        if r.status_code == 422:
            body = r.json()
            check("code == VALIDATION_ERROR", body.get("code") == "VALIDATION_ERROR")
            check(
                "错误里指出了是 email 字段",
                any("email" in e.get("field", "") for e in body.get("detail", {}).get("errors", [])),
                json.dumps(body, ensure_ascii=False),
            )

        # ── 4. 密码太短 ────────────────────────────────────
        print("\n[4] 密码太短（应该被拦住）")
        r = c.post("/auth/register", json={
            "username": f"short{suffix}", "email": f"short{suffix}@example.com",
            "password": "123",
        })
        check("返回 422", r.status_code == 422, f"实际 {r.status_code}")

        # ── 5. 登录成功 ────────────────────────────────────
        print("\n[5] 登录 POST /auth/login")
        r = c.post("/auth/login", json={"username": username, "password": password})
        check("返回 200", r.status_code == 200, f"实际 {r.status_code} {r.text[:200]}")

        token = None
        if r.status_code == 200:
            body = r.json()
            show("响应(截断)", {**body, "access_token": body["access_token"][:40] + "..."})
            token = body.get("access_token")
            check("拿到 access_token", bool(token))
            check("token_type == bearer", body.get("token_type") == "bearer")
            check("expires_in > 0", body.get("expires_in", 0) > 0)
            check("响应里带 user 信息", isinstance(body.get("user"), dict))
            check(
                "★ 登录响应里也没有 password_hash",
                "password_hash" not in json.dumps(body),
            )

        # ── 6. 用邮箱也能登录 ──────────────────────────────
        print("\n[6] 用邮箱登录（同一接口）")
        r = c.post("/auth/login", json={"username": email, "password": password})
        check("返回 200", r.status_code == 200, f"实际 {r.status_code}")

        # ── 7. 密码错误 ────────────────────────────────────
        print("\n[7] 密码错误")
        r = c.post("/auth/login", json={"username": username, "password": "wrongpassword"})
        check("返回 401", r.status_code == 401, f"实际 {r.status_code}")
        if r.status_code == 401:
            body = r.json()
            show("响应", body)
            check("code == UNAUTHORIZED", body.get("code") == "UNAUTHORIZED")
            check(
                "★ 提示是模糊的（不透露用户是否存在）",
                "用户名或密码错误" in body.get("message", ""),
                body.get("message", ""),
            )

        # ── 8. 不存在的用户 ────────────────────────────────
        print("\n[8] 用户不存在（提示应与密码错误【完全一样】）")
        r = c.post("/auth/login", json={
            "username": "nobody_at_all_here", "password": "whatever123",
        })
        check("返回 401", r.status_code == 401, f"实际 {r.status_code}")
        if r.status_code == 401:
            check(
                "★ message 与密码错误时完全相同（防用户名枚举）",
                r.json().get("message") == "用户名或密码错误",
                r.json().get("message", ""),
            )

        # ── 9. 带 token 取当前用户 ─────────────────────────
        print("\n[9] 获取当前用户 GET /auth/me")
        if token:
            r = c.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
            check("返回 200", r.status_code == 200, f"实际 {r.status_code}")
            if r.status_code == 200:
                show("响应", r.json())
                check("username 对得上", r.json().get("username") == username)

        # ── 10. 不带 token ─────────────────────────────────
        print("\n[10] 不带 token 访问 /auth/me")
        r = c.get("/auth/me")
        check("返回 401", r.status_code == 401, f"实际 {r.status_code}")
        if r.status_code == 401:
            body = r.json()
            show("响应", body)
            check(
                "★ 用的是我们自己的错误格式（不是 FastAPI 默认的 detail）",
                set(body) == {"code", "message", "detail"},
                json.dumps(body, ensure_ascii=False),
            )

        # ── 11. 伪造 token ─────────────────────────────────
        print("\n[11] 伪造 token")
        fake = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.fake_signature"
        r = c.get("/auth/me", headers={"Authorization": f"Bearer {fake}"})
        check("返回 401", r.status_code == 401, f"实际 {r.status_code}")
        if r.status_code == 401:
            check(
                "★ 签名校验拦住了伪造 token",
                r.json().get("code") == "UNAUTHORIZED",
                json.dumps(r.json(), ensure_ascii=False),
            )

        # ── 12. 格式错误的 Authorization 头 ────────────────
        print("\n[12] Authorization 头格式错误")
        r = c.get("/auth/me", headers={"Authorization": token or ""})
        check("返回 401（没写 Bearer 前缀）", r.status_code == 401, f"实际 {r.status_code}")

    total = passed + failed
    print(f"\n{'=' * 46}")
    if failed == 0:
        print(f"{GREEN}全部通过{RESET}  {passed}/{total}")
    else:
        print(f"{RED}失败 {failed}{RESET} / 共 {total}   {GREEN}通过 {passed}{RESET}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
