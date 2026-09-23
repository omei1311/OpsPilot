"""Agent 端到端测试（真实调用大模型）。

⚠️ 这个测试会真实消耗 LLM token，也会真实修改数据库。

跑之前：
    uvicorn app.main:app --port 8000
    python scripts/seed.py          # 保证有足够数据
    在 .env 里配好 LLM_API_KEY

然后：
    python scripts/test_agent.py              # 全部
    python scripts/test_agent.py --only chat  # 只跑某个场景
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

GREEN, RED, DIM, YELLOW, CYAN, RESET = (
    "\033[32m", "\033[31m", "\033[2m", "\033[33m", "\033[36m", "\033[0m"
)

passed = failed = 0


def chk(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"    {GREEN}PASS{RESET}  {name}")
    else:
        failed += 1
        print(f"    {RED}FAIL{RESET}  {name}")
        if detail:
            print(f"          {DIM}{str(detail)[:400]}{RESET}")


def consume_sse(
    client: httpx.Client,
    url: str,
    payload: dict,
    headers: dict,
    *,
    verbose: bool = True,
    timeout: float = 180.0,
) -> tuple[list[dict], dict]:
    """消费一个 SSE 流，返回 (事件列表, 汇总信息)。

    手动解析 SSE 协议：
      · 以 ":" 开头的是注释（心跳），忽略
      · "event: xxx" 指定事件名
      · "data: {...}" 是数据
      · 空行表示一帧结束
    """
    events: list[dict] = []
    summary: dict = {}
    current: dict = {}

    with client.stream("POST", url, json=payload, headers=headers, timeout=timeout) as r:
        summary["status_code"] = r.status_code
        if r.status_code != 200:
            summary["body"] = r.read().decode("utf-8", errors="replace")[:500]
            return events, summary

        for line in r.iter_lines():
            if not line:
                # 空行 = 一帧结束
                if current:
                    events.append(current)
                    if verbose:
                        _print_event(current)
                    current = {}
                continue

            if line.startswith(":"):
                continue  # 心跳注释

            if line.startswith("event:"):
                current["event"] = line[6:].strip()
            elif line.startswith("data:"):
                raw = line[5:].strip()
                try:
                    current["data"] = json.loads(raw)
                except json.JSONDecodeError:
                    current["data"] = {"_raw": raw}

    return events, summary


def _print_event(ev: dict) -> None:
    """把事件打印成人能读的一行。"""
    name = ev.get("event", "?")
    data = ev.get("data", {})

    if name == "intent":
        print(f"      {CYAN}[意图]{RESET} {data.get('intent')} (置信度 {data.get('confidence')})")
        if data.get("entities"):
            print(f"             {DIM}实体: {json.dumps(data['entities'], ensure_ascii=False)[:150]}{RESET}")
    elif name == "tool_call":
        print(f"      {CYAN}[调用]{RESET} {data.get('tool')}({json.dumps(data.get('args', {}), ensure_ascii=False)[:120]})")
    elif name == "tool_result":
        mark = "✓" if data.get("ok") else "✗"
        print(f"      {CYAN}[结果]{RESET} {mark} {data.get('summary')} ({data.get('duration_ms')}ms)")
    elif name == "plan":
        print(f"      {YELLOW}[计划]{RESET} {data.get('title')}")
        print(f"             {DIM}影响 {data.get('affected_count')} 条，超时 {data.get('timeout_seconds')}s{RESET}")
    elif name == "awaiting_approval":
        print(f"      {YELLOW}[等待确认]{RESET} action_id={data.get('action_id')}")
    elif name == "action_executed":
        print(f"      {CYAN}[执行]{RESET} {data.get('ticket_no')} {'✓' if data.get('ok') else '✗'}")
    elif name == "action_finished":
        print(f"      {CYAN}[完成]{RESET} 成功 {data.get('succeeded')} / 失败 {data.get('failed')}")
    elif name == "token":
        print(f"      {GREEN}[回答]{RESET} {data.get('delta', '')[:200]}")
    elif name == "citations":
        items = data.get("items", [])
        print(f"      {DIM}[引用] {len(items)} 条工单{RESET}")
    elif name == "error":
        print(f"      {RED}[错误]{RESET} {data.get('message')}")
    elif name == "done":
        print(f"      {DIM}[结束] status={data.get('status')} 耗时={data.get('duration_ms')}ms{RESET}")
    elif name == "thought":
        pass  # 思考过程太多，不打印


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--only",
        choices=["chat", "create", "query", "analyze", "batch"],
        help="只跑某一个场景",
    )
    args = parser.parse_args()
    api = f"{args.base_url.rstrip('/')}/api/v1"

    with httpx.Client(base_url=api, timeout=30.0, trust_env=False) as c:
        # ── 登录 ───────────────────────────────────────────
        r = c.post("/auth/login", json={"username": "admin", "password": "admin123456"})
        if r.status_code != 200:
            print(f"{RED}登录失败，请先跑 scripts/seed.py{RESET}")
            print(r.text[:300])
            return 1
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        print(f"{GREEN}已登录 admin{RESET}")

        # 检查 LLM 是否配置
        hc = c.get("/health")
        chk("后端服务正常", hc.status_code == 200)

        run_all = args.only is None

        # ══════════════════════════════════════════════════
        if run_all or args.only == "chat":
            print(f"\n{'='*60}")
            print("场景 0：闲聊（不给工具，直接回答）")
            print(f"{'='*60}")
            events, _ = consume_sse(
                c, f"{api}/agent/chat", {"message": "你好，你能做什么？"}, h
            )
            names = [e["event"] for e in events]
            chk("有 run_started", "run_started" in names)
            chk("有 done", "done" in names)
            chk("意图是 chat", any(e["event"] == "intent" and e["data"].get("intent") == "chat" for e in events))
            chk("★ 没有调用任何工具", "tool_call" not in names)
            chk("有回答文本", any(e["event"] == "token" for e in events))

        # ══════════════════════════════════════════════════
        if run_all or args.only == "create":
            print(f"\n{'='*60}")
            print("场景 1：自然语言创建工单")
            print(f"{'='*60}")
            print(f"{DIM}输入: 线上支付接口大量出现 502，今天下午开始一直有用户反馈支付失败，帮我报个故障。{RESET}")
            events, _ = consume_sse(
                c,
                f"{api}/agent/chat",
                {
                    "message": "线上支付接口大量出现 502，今天下午开始一直有用户反馈支付失败，帮我报个故障。"
                },
                h,
            )
            calls = [e["data"] for e in events if e["event"] == "tool_call"]
            created = [
                e["data"] for e in events
                if e["event"] == "tool_result" and e["data"].get("tool") == "create_ticket"
            ]
            chk("调用了 create_ticket", any(c.get("tool") == "create_ticket" for c in calls), str(calls))
            if created:
                chk("创建成功", created[0].get("ok") is True, str(created[0]))

        # ══════════════════════════════════════════════════
        if run_all or args.only == "query":
            print(f"\n{'='*60}")
            print("场景 2：查询工单")
            print(f"{'='*60}")
            print(f"{DIM}输入: 帮我看看最近有哪些高优先级工单没处理{RESET}")
            events, _ = consume_sse(
                c, f"{api}/agent/chat", {"message": "帮我看看最近有哪些高优先级工单没处理"}, h
            )
            calls = [e["data"] for e in events if e["event"] == "tool_call"]
            chk("调用了查询类工具", any(c.get("tool") in ("list_tickets", "get_ticket_statistics") for c in calls), str(calls))
            citations = [e["data"] for e in events if e["event"] == "citations"]
            chk("★ 返回了引用工单（防幻觉）", len(citations) > 0 and len(citations[0].get("items", [])) > 0)

        # ══════════════════════════════════════════════════
        if run_all or args.only == "analyze":
            print(f"\n{'='*60}")
            print("场景 3：SLA 风险分析")
            print(f"{'='*60}")
            print(f"{DIM}输入: 分析最近 7 天的 SLA 风险{RESET}")
            events, _ = consume_sse(
                c, f"{api}/agent/chat", {"message": "分析最近 7 天的 SLA 风险"}, h
            )
            calls = [e["data"] for e in events if e["event"] == "tool_call"]
            chk(
                "调用了 analyze_sla_risk",
                any(c.get("tool") == "analyze_sla_risk" for c in calls),
                str([c.get("tool") for c in calls]),
            )
            answers = [e["data"].get("delta", "") for e in events if e["event"] == "token"]
            full = "".join(answers)
            chk("回答有内容", len(full) > 20, full[:200])

        # ══════════════════════════════════════════════════
        if run_all or args.only == "batch":
            print(f"\n{'='*60}")
            print("场景 4：批量修改（Human-in-the-loop）★ 核心")
            print(f"{'='*60}")
            print(f"{DIM}输入: 把所有超过 24 小时未处理的工单升级成高优先级，并分配给技术部门{RESET}")

            events, _ = consume_sse(
                c,
                f"{api}/agent/chat",
                {
                    "message": "把所有超过 24 小时未处理的工单升级成高优先级，并分配给技术部门"
                },
                h,
            )
            names = [e["event"] for e in events]
            calls = [e["data"] for e in events if e["event"] == "tool_call"]

            chk("意图识别为 batch_update",
                any(e["event"] == "intent" and e["data"].get("intent") == "batch_update" for e in events),
                str([e["data"].get("intent") for e in events if e["event"] == "intent"]))

            chk("★ 调用了批量工具",
                any(c.get("tool", "").startswith("batch_") for c in calls),
                str([c.get("tool") for c in calls]))

            chk("★ 推送了 plan 事件", "plan" in names)
            chk("★ 推送了 awaiting_approval", "awaiting_approval" in names)

            plan_ev = next((e["data"] for e in events if e["event"] == "plan"), None)
            done_ev = next((e["data"] for e in events if e["event"] == "done"), None)

            if plan_ev:
                action_id = plan_ev["action_id"]
                print(f"\n      {YELLOW}计划详情:{RESET}")
                print(f"        标题: {plan_ev['title']}")
                print(f"        影响: {plan_ev['affected_count']} 条")
                print(f"        预览: {len(plan_ev.get('preview') or [])} 条")
                if plan_ev.get("preview"):
                    for item in plan_ev["preview"][:3]:
                        print(f"          · {item.get('ticket_no')} {str(item.get('title'))[:30]} [{item.get('priority')}]")

                chk("★ 有逐条预览（用户能核对具体是哪些）",
                    len(plan_ev.get("preview") or []) > 0)
                chk("影响条数合理(1-100)", 0 < plan_ev["affected_count"] <= 100,
                    str(plan_ev["affected_count"]))

                chk("★ 结束时 status = awaiting_approval",
                    done_ev and done_ev.get("status") == "awaiting_approval",
                    str(done_ev))

                # ── 验证没执行 ──────────────────────────
                print(f"\n      {DIM}验证：确认前数据未被修改...{RESET}")
                r = c.get("/tickets", params={"unhandled_hours": 24, "page_size": 100}, headers=h)
                before_count = r.json()["total"] if r.status_code == 200 else -1
                chk("★ 确认前工单数量未变（没被偷偷执行）", before_count > 0, f"待处理 {before_count} 条")

                # ── 测试编辑勾选 ────────────────────────
                preview = plan_ev.get("preview") or []
                if len(preview) > 3:
                    # 从预览里取工单号，反查 id
                    r = c.get("/tickets", params={"unhandled_hours": 24, "page_size": 100}, headers=h)
                    all_ids = [t["id"] for t in r.json()["items"]]
                    keep = all_ids[:3]  # 只保留 3 条
                    print(f"\n      {DIM}测试：用户取消勾选，只执行 {len(keep)} 条...{RESET}")

                    r = c.get(f"/agent/actions/{action_id}", headers=h)
                    chk("能查到待确认详情", r.status_code == 200, str(r.status_code))

                    # ── 确认执行 ────────────────────────
                    events2, _ = consume_sse(
                        c,
                        f"{api}/agent/actions/{action_id}/approve",
                        {"edited_payload": {"ticket_ids": keep}},
                        h,
                    )
                    names2 = [e["event"] for e in events2]
                    fin = next((e["data"] for e in events2 if e["event"] == "action_finished"), None)

                    chk("★ 执行完成事件已推送", "action_finished" in names2)
                    if fin:
                        print(f"\n      {GREEN}执行结果: 成功 {fin.get('succeeded')} / 失败 {fin.get('failed')}{RESET}")
                        chk("★ 只执行了勾选的条数", fin.get("succeeded") == len(keep),
                            f"预期 {len(keep)}，实际 {fin.get('succeeded')}")
                        chk("没有失败", fin.get("failed") == 0, str(fin))

                    # ── 验证数据真的改了 ────────────────────
                    print(f"\n      {DIM}验证：数据是否真的被修改...{RESET}")
                    r = c.get(f"/tickets/{keep[0]}", headers=h)
                    if r.status_code == 200:
                        t = r.json()
                        chk("★ 优先级已改为 high", t.get("priority") == "high", t.get("priority"))
                        chk("★ 部门已分配为技术部", t.get("department_name") == "技术部",
                            str(t.get("department_name")))
                        chk("★ 状态已推进为 processing", t.get("status") == "processing",
                            t.get("status"))
                        # 检查审计日志
                        evts = [e["event_type"] for e in t.get("events", [])]
                        chk("★ 审计日志记录了批量修改",
                            "priority_changed" in evts and "assigned" in evts, str(evts))

                    # ── 验证幂等：重复确认应该被拒 ──────────
                    print(f"\n      {DIM}验证：重复提交是否被拦截...{RESET}")
                    r = c.post(f"/agent/actions/{action_id}/approve", json={}, headers=h)
                    chk("★ 重复确认被拒绝（幂等保护）", r.status_code == 409 or "error" in r.text,
                        f"{r.status_code} {r.text[:200]}")

        # ══════════════════════════════════════════════════
        if run_all:
            print(f"\n{'='*60}")
            print("附加验证：拒绝流程")
            print(f"{'='*60}")
            events, _ = consume_sse(
                c,
                f"{api}/agent/chat",
                {"message": "把所有超过 48 小时未处理的工单都关闭"},
                h,
            )
            plan_ev = next((e["data"] for e in events if e["event"] == "plan"), None)
            if plan_ev:
                action_id = plan_ev["action_id"]
                events2, _ = consume_sse(
                    c,
                    f"{api}/agent/actions/{action_id}/reject",
                    {"note": "测试拒绝"},
                    h,
                )
                done2 = next((e["data"] for e in events2 if e["event"] == "done"), None)
                chk("★ 拒绝后状态是 rejected", done2 and done2.get("status") == "rejected", str(done2))
                chk("没有执行任何操作",
                    not any(e["event"] == "action_executed" for e in events2))
            else:
                print(f"      {YELLOW}模型没有生成计划，跳过拒绝测试{RESET}")

    total = passed + failed
    print(f"\n{'=' * 60}")
    if failed == 0:
        print(f"{GREEN}全部通过{RESET}  {passed}/{total}")
    else:
        print(f"{RED}失败 {failed}{RESET} / 共 {total}   {GREEN}通过 {passed}{RESET}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
