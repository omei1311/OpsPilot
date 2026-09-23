"""前端页面编译验证 + 全栈链路检查。

浏览器渲染没法用脚本验证，但可以验证「Vite 能不能把这些 .vue 编译通过」——
编译失败是前端最常见的错误来源，这一步能挡住绝大多数问题。

前提：前后端都已启动
    backend:  uvicorn app.main:app --port 8000
    frontend: npm run dev
"""

from __future__ import annotations

import pathlib
import sys

import httpx

# 让脚本能 import app 包
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)

# 是否配了大模型 —— 没配的话 SSE 部分跳过（那部分会真的调模型、花 token）
LLM_CONFIGURED = settings.llm_configured

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


# 需要能编译通过的前端模块
MODULES = [
    "/src/main.ts",
    "/src/App.vue",
    "/src/layouts/MainLayout.vue",
    "/src/views/LoginView.vue",
    "/src/views/RegisterView.vue",
    "/src/views/DashboardView.vue",
    "/src/views/NotFoundView.vue",
    "/src/views/ticket/TicketListView.vue",
    "/src/views/ticket/TicketDetailView.vue",
    "/src/components/ticket/TicketTags.vue",
    "/src/api/request.ts",
    "/src/api/auth.ts",
    "/src/api/ticket.ts",
    "/src/api/dashboard.ts",
    "/src/stores/user.ts",
    "/src/router/index.ts",
    "/src/types/ticket.ts",
    # ── Agent Copilot（块 4）──────────────────────────────
    "/src/api/sse.ts",
    "/src/api/agent.ts",
    "/src/stores/agent.ts",
    "/src/types/agent.ts",
    "/src/components/agent/AgentPanel.vue",
    "/src/components/agent/AgentStepTimeline.vue",
    "/src/components/agent/ActionConfirmCard.vue",
]


def main() -> int:
    fe = "http://127.0.0.1:5173"

    # ⚠️ trust_env=False 是必须的。
    #
    # httpx 默认会读取系统代理设置 —— 在 Windows 上是从【注册表】读的，
    # 不只是环境变量。如果机器上开着 Clash / V2Ray 这类工具，
    # 访问 127.0.0.1 的请求也会被转发给代理，结果就是莫名其妙的 502。
    #
    # curl.exe 不读注册表代理，所以会出现"curl 正常但 httpx 报错"的怪现象。
    # 测本机服务时一律关掉环境代理。
    with httpx.Client(timeout=30.0, trust_env=False) as c:
        print("\n[1] 前端服务")
        r = c.get(f"{fe}/")
        chk("index.html 可访问", r.status_code == 200, str(r.status_code))

        print("\n[2] Vue 组件 / TS 模块编译")
        for path in MODULES:
            try:
                r = c.get(f"{fe}{path}")
                ok = r.status_code == 200 and not r.text.lstrip().startswith("<!DOCTYPE")
                # Vite 编译失败时会返回 500 或把错误信息塞在响应里
                if r.status_code == 200 and "Internal server error" in r.text:
                    ok = False
                chk(f"{path}", ok, f"{r.status_code} {r.text[:150]}")
            except Exception as exc:  # noqa: BLE001
                chk(f"{path}", False, str(exc))

        print("\n[3] 后端接口（经 Vite 代理）")
        api = f"{fe}/api/v1"
        r = c.get(f"{api}/health")
        chk("/health 正常", r.status_code == 200, str(r.status_code))

        r = c.post(f"{api}/auth/login", json={"username": "admin", "password": "admin123456"})
        chk("admin 登录成功", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

        if r.status_code != 200:
            print(f"\n{RED}登录失败，后面的检查跳过。请先跑 scripts/seed.py{RESET}")
            return 1

        token = r.json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}

        print("\n[4] Dashboard 接口")
        for path, label in [
            ("/dashboard/overview", "概览"),
            ("/dashboard/trend?days=7", "趋势"),
            ("/dashboard/distribution", "分布"),
            ("/dashboard/sla-risk?limit=8", "SLA 风险"),
            ("/dashboard/workload", "工作量"),
        ]:
            r = c.get(f"{api}{path}", headers=h)
            chk(f"{label} {path}", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

        trend = c.get(f"{api}/dashboard/trend?days=7", headers=h).json()
        chk("★ 趋势数据补齐了 7 个点（含没有数据的日期）", len(trend["points"]) == 7, str(len(trend["points"])))

        print("\n[5] 工单接口")
        r = c.get(f"{api}/tickets", params={"page_size": 5}, headers=h)
        chk("列表返回 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            body = r.json()
            print(f"        {DIM}共 {body['total']} 条工单{RESET}")
            chk("有数据可展示", body["total"] > 0)

            if body["items"]:
                tid = body["items"][0]["id"]
                r = c.get(f"{api}/tickets/{tid}", headers=h)
                chk(f"详情 /tickets/{tid} 返回 200", r.status_code == 200, str(r.status_code))
                if r.status_code == 200:
                    d = r.json()
                    chk("详情含 comments 字段", "comments" in d)
                    chk("详情含 events 字段", "events" in d)

        r = c.get(f"{api}/tickets", params={"unhandled_hours": 24, "page_size": 100}, headers=h)
        chk("场景 4 筛选取到数据", r.status_code == 200 and r.json()["total"] > 0, str(r.status_code))
        print(f"        {DIM}超过 24 小时未处理：{r.json()['total']} 条{RESET}")

        # ── 6. ★ SSE 经过 Vite 代理能否真正流式 ────────────
        #
        # 这一步很重要：开发代理和 nginx 都有可能把 SSE 流缓冲起来，
        # 表现是"前端一直转圈，最后一次性全部出现"—— 完全失去流式的意义。
        # 这里验证：经代理拿到的第一个事件，应该明显早于整个响应结束。
        print("\n[6] SSE 流式（经 Vite 代理）")
        if not LLM_CONFIGURED:
            print(f"        {YELLOW}未配置 LLM_API_KEY，跳过{RESET}")
        else:
            import time as _time

            first_event_at = None
            events: list[str] = []
            started = _time.perf_counter()

            try:
                with c.stream(
                    "POST",
                    f"{api}/agent/chat",
                    json={"message": "你好"},
                    headers=h,
                    timeout=120.0,
                ) as resp:
                    chk("SSE 端点返回 200", resp.status_code == 200, str(resp.status_code))

                    if resp.status_code == 200:
                        # 检查响应头 —— 没有这个头的话 nginx 会缓冲
                        chk(
                            "响应头 Content-Type 是 text/event-stream",
                            "text/event-stream" in resp.headers.get("content-type", ""),
                            resp.headers.get("content-type", ""),
                        )
                        chk(
                            "★ 带 X-Accel-Buffering: no（防 nginx 缓冲）",
                            resp.headers.get("x-accel-buffering") == "no",
                            str(resp.headers.get("x-accel-buffering")),
                        )

                        for line in resp.iter_lines():
                            if line.startswith("event:"):
                                name = line[6:].strip()
                                if name not in events:
                                    events.append(name)
                                if first_event_at is None and name:
                                    first_event_at = _time.perf_counter() - started
                                if name == "done":
                                    break

                        total = _time.perf_counter() - started
                        print(f"        {DIM}收到事件: {', '.join(events)}{RESET}")

                        chk("收到了事件", len(events) > 0, str(events))
                        chk("第一个事件是 run_started", events and events[0] == "run_started", str(events[:3]))
                        chk("最后一个事件是 done", "done" in events, str(events[-3:]))
                        if first_event_at is not None:
                            chk(
                                "★ 首事件到达远早于流结束（说明真的在流式，没被缓冲）",
                                first_event_at < total * 0.8,
                                f"首事件 {first_event_at:.2f}s / 总耗时 {total:.2f}s",
                            )
            except Exception as exc:  # noqa: BLE001
                chk("SSE 流式请求成功", False, f"{type(exc).__name__}: {exc}")

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
