"""全栈链路验证：前端 → Vite 代理 → 后端 → MySQL。

前提：前后端都已启动
    backend:  uvicorn app.main:app --port 8000
    frontend: npm run dev          (5173)

然后：
    python scripts/verify_full_stack.py
"""

from __future__ import annotations

import json
import random
import string
import sys

import httpx

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

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
            print(f"        {DIM}{detail[:300]}{RESET}")


def main() -> int:
    fe = "http://127.0.0.1:5173"
    api = f"{fe}/api/v1"

    with httpx.Client(timeout=20.0, trust_env=False) as c:
        # ── 1. 前端页面 ────────────────────────────────────
        print("\n[1] 前端页面")
        r = c.get(f"{fe}/")
        chk("GET / 返回 200", r.status_code == 200, str(r.status_code))
        chk('返回的是 index.html', '<div id="app">' in r.text, r.text[:200])
        chk("入口脚本被引用", "/src/main.ts" in r.text)

        # ── 2. Vite 能编译 Vue 组件 ────────────────────────
        print("\n[2] Vite 编译 Vue 单文件组件")
        r = c.get(f"{fe}/src/views/LoginView.vue")
        chk("LoginView.vue 编译成功", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            chk(
                "返回的是编译后的 JS（不是原始 .vue 源码）",
                "_sfc_main" in r.text or "export default" in r.text,
                r.text[:200],
            )
        r = c.get(f"{fe}/src/main.ts")
        chk("main.ts 可加载", r.status_code == 200, str(r.status_code))

        # ── 3. Vite 代理 ───────────────────────────────────
        print("\n[3] Vite 代理：前端 /api → 后端 8000")
        r = c.get(f"{api}/health")
        chk("经代理访问 /api/v1/health 返回 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            print(f"        {DIM}{json.dumps(r.json(), ensure_ascii=False)}{RESET}")

        r = c.get(f"{api}/health/db")
        chk("经代理访问数据库检查返回 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            body = r.json()
            chk("代理链路能真的读到 MySQL", bool(body.get("server_version")), str(body))
            print(f"        {DIM}mysql {body.get('server_version')} / {body.get('latency_ms')}ms{RESET}")

        # ── 4. 完整链路：注册 + 登录 + 取用户 ──────────────
        print("\n[4] 完整链路：注册 → 登录 → 带 token 取用户")
        sfx = "".join(random.choices(string.digits, k=6))
        uname, pwd = f"e2e{sfx}", "ops123456"

        r = c.post(
            f"{api}/auth/register",
            json={"username": uname, "email": f"e2e{sfx}@example.com", "password": pwd},
        )
        chk("注册成功 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")

        r = c.post(f"{api}/auth/login", json={"username": uname, "password": pwd})
        chk("登录成功 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        token = r.json().get("access_token") if r.status_code == 200 else None
        chk("拿到 access_token", bool(token))

        if token:
            r = c.get(f"{api}/auth/me", headers={"Authorization": f"Bearer {token}"})
            chk("带 token 取用户成功", r.status_code == 200, str(r.status_code))
            if r.status_code == 200:
                data = r.json()
                print(f"        {DIM}{json.dumps(data, ensure_ascii=False)}{RESET}")
                chk("返回的正是刚注册的用户", data.get("username") == uname)
                chk("响应里没有 password_hash", "password_hash" not in json.dumps(data))

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
