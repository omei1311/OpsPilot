"""端到端冒烟测试：对着真实运行的服务打一遍关键接口。

用途是在「启动 uvicorn 之后」快速确认整条链路通不通，
比 pytest 更接近真实部署形态（真的走 HTTP、真的连 MySQL）。

用法::

    # 另开一个终端启动服务
    uvicorn app.main:app --port 8000

    # 然后跑这个脚本
    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

# 终端着色，Windows Terminal / PowerShell 7 都支持 ANSI
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    with httpx.Client(base_url=base, timeout=10.0) as c:
        # ── 1. 根路径 ──────────────────────────────────────
        print("\n[1] 根路径")
        r = c.get("/")
        check("GET / 返回 200", r.status_code == 200, f"实际 {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            check("包含 app 字段", "app" in body, json.dumps(body, ensure_ascii=False))

        # ── 2. 存活检查（不依赖数据库）───────────────────────
        print("\n[2] 存活检查 GET /api/v1/health")
        r = c.get("/api/v1/health")
        check("返回 200", r.status_code == 200, f"实际 {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            check("status == ok", body.get("status") == "ok", json.dumps(body, ensure_ascii=False))
            check("time 是 ISO 8601", "T" in body.get("time", ""), body.get("time", ""))
            check(
                "不含 database 字段（说明未依赖数据库）",
                "database" not in body,
                json.dumps(body, ensure_ascii=False),
            )

        # ── 3. 数据库检查 ──────────────────────────────────
        print("\n[3] 数据库检查 GET /api/v1/health/db")
        r = c.get("/api/v1/health/db")
        check("返回 200", r.status_code == 200, f"实际 {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            check("database == mysql", body.get("database") == "mysql", str(body.get("database")))
            check("返回服务端版本", bool(body.get("server_version")), str(body))
            check("latency_ms >= 0", body.get("latency_ms", -1) >= 0, str(body.get("latency_ms")))
            pool = body.get("pool") or {}
            check("overflow >= 0（不能是负数）", pool.get("overflow", -1) >= 0, str(pool))
        elif r.status_code == 503:
            body = r.json()
            check(
                "数据库不可用时返回 503 + DATABASE_UNAVAILABLE",
                body.get("code") == "DATABASE_UNAVAILABLE",
                json.dumps(body, ensure_ascii=False),
            )

        # ── 4. 统一错误格式 ────────────────────────────────
        print("\n[4] 统一错误格式")
        r = c.get("/api/v1/definitely-not-a-route")
        check("未知路由返回 404", r.status_code == 404, f"实际 {r.status_code}")
        body = r.json()
        check(
            "响应体是 {code, message, detail}",
            set(body) == {"code", "message", "detail"},
            json.dumps(body, ensure_ascii=False),
        )
        check("code == ROUTE_NOT_FOUND", body.get("code") == "ROUTE_NOT_FOUND", str(body))

        r = c.post("/api/v1/health")
        check("方法不允许返回 405", r.status_code == 405, f"实际 {r.status_code}")
        check(
            "405 也用统一格式",
            r.json().get("code") == "METHOD_NOT_ALLOWED",
            json.dumps(r.json(), ensure_ascii=False),
        )

        # ── 5. 接口文档 ────────────────────────────────────
        print("\n[5] 接口文档")
        check("GET /docs 可访问", c.get("/docs").status_code == 200)
        r = c.get("/api/v1/openapi.json")
        check("OpenAPI schema 可获取", r.status_code == 200, f"实际 {r.status_code}")
        if r.status_code == 200:
            paths = r.json().get("paths", {})
            check("包含 /api/v1/health", "/api/v1/health" in paths, str(sorted(paths)))
            check("包含 /api/v1/health/db", "/api/v1/health/db" in paths)

        # ── 6. CORS ───────────────────────────────────────
        print("\n[6] CORS 预检")
        r = c.options(
            "/api/v1/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        check("预检返回 200", r.status_code == 200, f"实际 {r.status_code}")
        check(
            "回显 Access-Control-Allow-Origin",
            r.headers.get("access-control-allow-origin") == "http://localhost:5173",
            str(dict(r.headers)),
        )

    # ── 汇总 ───────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'=' * 46}")
    if failed == 0:
        print(f"{GREEN}全部通过{RESET}  {passed}/{total}")
    else:
        print(f"{RED}失败 {failed}{RESET} / 共 {total}   {YELLOW}通过 {passed}{RESET}")
    print("=" * 46)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
