"""Agent 工具测试（不需要 LLM API Key）。

★ 这是本阶段最重要的验证。

为什么工具能脱离 LLM 单独测？
    因为工具是【纯函数】：给它参数和上下文，它返回结构化结果。
    "决定调哪个工具、传什么参数"才是 LLM 的活。

    这个可测试性正是"Tool 和 Agent 分开"的价值：
      · 工具的正确性可以用传统单测保证（本文件）
      · LLM 的不确定性被隔离在"选工具"这一层
    出问题时分得清是工具的锅还是模型的锅。

跑之前先启动后端并灌入种子数据：
    uvicorn app.main:app --port 8000
    python scripts/seed.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.agent.context import ToolContext  # noqa: E402
from app.agent.policy import assess_risk  # noqa: E402
from app.agent.tools import ALL_TOOLS, INTENT_TOOLS, TOOL_MAP, get_tools_for_intent  # noqa: E402
from app.db.session import AsyncSessionLocal, dispose_engine  # noqa: E402
from app.models.user import User  # noqa: E402

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
            print(f"        {DIM}{str(detail)[:400]}{RESET}")


async def call(tool_name: str, args: dict, ctx: ToolContext) -> dict:
    """调用一个工具，自动带上上下文。

    注意这里模拟的就是 graph 调用工具的方式：
    config 里带 ToolContext，而【参数里没有身份信息】。
    """
    tool = TOOL_MAP[tool_name]
    return await tool.ainvoke(args, config=ctx.to_config())


async def main() -> int:
    async with AsyncSessionLocal() as db:
        # ── 准备两个身份的上下文 ───────────────────────────
        admin = (
            await db.execute(select(User).where(User.username == "admin"))
        ).scalars().first()
        zhangsan = (
            await db.execute(select(User).where(User.username == "zhangsan"))
        ).scalars().first()

        if admin is None or zhangsan is None:
            print(f"{RED}找不到演示用户，请先跑 scripts/seed.py{RESET}")
            return 1

        admin_ctx = ToolContext(db=db, user=admin, run_id="test-run-admin")
        user_ctx = ToolContext(db=db, user=zhangsan, run_id="test-run-user")

        # ══════════════════════════════════════════════════
        print("\n[1] 工具注册表")
        # ══════════════════════════════════════════════════
        chk(f"共注册 {len(ALL_TOOLS)} 个工具", len(ALL_TOOLS) >= 9, str(len(ALL_TOOLS)))
        names = [t.name for t in ALL_TOOLS]
        print(f"        {DIM}{', '.join(names)}{RESET}")

        required = {
            "create_ticket", "get_ticket", "list_tickets", "update_ticket",
            "assign_ticket", "add_ticket_comment", "get_ticket_statistics",
            "analyze_sla_risk", "summarize_ticket",
        }
        missing = required - set(names)
        chk("架构文档要求的 9 个工具都在", not missing, f"缺失: {missing}")

        for t in ALL_TOOLS:
            chk(f"  {t.name} 有描述文字", bool(t.description and len(t.description) > 20))

        # ══════════════════════════════════════════════════
        print("\n[2] ★ 身份隔离：工具的 schema 里【不能】出现 user_id")
        # ══════════════════════════════════════════════════
        for t in ALL_TOOLS:
            schema = t.args_schema.model_json_schema()
            props = set(schema.get("properties", {}).keys())
            leaked = {p for p in props if p in ("user_id", "config", "ctx", "creator_id")}
            chk(f"  {t.name} 没泄漏身份/上下文参数", not leaked, f"多出: {leaked}")

        # ══════════════════════════════════════════════════
        print("\n[3] 意图 → 工具白名单")
        # ══════════════════════════════════════════════════
        chk("chat 意图不暴露任何工具", len(get_tools_for_intent("chat")) == 0)
        chk(
            "create_ticket 意图只暴露少量工具",
            0 < len(get_tools_for_intent("create_ticket")) <= 3,
            str([t.name for t in get_tools_for_intent("create_ticket")]),
        )
        chk("未知意图返回空列表（fail-safe）", len(get_tools_for_intent("不存在的意图")) == 0)
        print(f"        {DIM}意图清单: {', '.join(INTENT_TOOLS.keys())}{RESET}")

        # ══════════════════════════════════════════════════
        print("\n[4] 读工具：list_departments")
        # ══════════════════════════════════════════════════
        r = await call("list_departments", {}, admin_ctx)
        chk("返回成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:300])
        depts = r.get("departments", [])
        chk("拿到 4 个部门", len(depts) == 4, str(depts))
        print(f"        {DIM}{json.dumps(depts, ensure_ascii=False)}{RESET}")
        tech_id = next((d["id"] for d in depts if d["name"] == "技术部"), None)
        chk("★ 技术部可被名称映射到 id（场景 4 前提）", tech_id is not None)

        # ══════════════════════════════════════════════════
        print("\n[5] 读工具：list_tickets")
        # ══════════════════════════════════════════════════
        r = await call("list_tickets", {"limit": 5}, admin_ctx)
        chk("返回成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:300])
        chk("返回了数据", r.get("count", 0) > 0, str(r.get("count")))
        print(f"        {DIM}总数 {r.get('total')}，本次返回 {r.get('count')} 条{RESET}")
        if r.get("items"):
            print(f"        {DIM}示例: {json.dumps(r['items'][0], ensure_ascii=False)}{RESET}")

        # ★ 场景 4 的核心查询
        r = await call("list_tickets", {"unhandled_hours": 24, "limit": 50}, admin_ctx)
        chk("★ unhandled_hours=24 能筛出工单", r.get("total", 0) > 0, str(r.get("total")))
        print(f"        {DIM}超过 24 小时未处理: {r.get('total')} 条{RESET}")

        # limit 上限保护
        #
        # 上限是 100（和 AGENT_BATCH_LIMIT 对齐）。
        # 不能设太小 —— 场景 4 里用户说"所有超过 24 小时的工单"，
        # 如果上限只有 50 而实际有 67 条，就会静默漏掉 17 条，
        # 而且不会有任何报错。这个坑在 test_agent.py 场景 4 里真实踩到过。
        r = await call("list_tickets", {"limit": 9999}, admin_ctx)
        chk("★ limit 被夹到 100 以内（防上下文爆炸）", r.get("count", 0) <= 100, str(r.get("count")))

        # truncated 字段必须存在 —— 提示词依赖它判断"要不要继续翻页"
        r = await call("list_tickets", {"limit": 5}, admin_ctx)
        chk("★ 返回值含 truncated 字段", "truncated" in r, str(list(r.keys())))
        chk("★ 只取 5 条时 truncated 为 true", r.get("truncated") is True, str(r.get("truncated")))

        # ══════════════════════════════════════════════════
        print("\n[6] ★ 数据权限：普通用户看到的比 admin 少")
        # ══════════════════════════════════════════════════
        r_admin = await call("list_tickets", {"limit": 1}, admin_ctx)
        r_user = await call("list_tickets", {"limit": 1}, user_ctx)
        chk(
            "zhangsan 可见总数 < admin 可见总数",
            r_user.get("total", 0) < r_admin.get("total", 0),
            f"user={r_user.get('total')} admin={r_admin.get('total')}",
        )
        print(f"        {DIM}admin 可见 {r_admin.get('total')} 条 / zhangsan 可见 {r_user.get('total')} 条{RESET}")

        # ══════════════════════════════════════════════════
        print("\n[7] 读工具：统计 / SLA 分析")
        # ══════════════════════════════════════════════════
        r = await call("get_ticket_statistics", {"days": 30}, admin_ctx)
        chk("统计返回成功", r.get("ok") is True, str(r)[:200])
        chk("含 by_status", bool(r.get("by_status")))
        print(f"        {DIM}total={r.get('total')} overdue={r.get('overdue')} unassigned={r.get('unassigned')}{RESET}")

        r = await call("analyze_sla_risk", {"days": 7, "limit": 5}, admin_ctx)
        chk("SLA 分析返回成功", r.get("ok") is True, str(r)[:200])
        chk("含超时统计", "total_overdue" in r, str(r.keys()))
        print(f"        {DIM}超时 {r.get('total_overdue')} 条，即将超时 {r.get('total_at_risk')} 条{RESET}")

        # ══════════════════════════════════════════════════
        print("\n[8] ★ 写工具：create_ticket（场景 1）")
        # ══════════════════════════════════════════════════
        r = await call(
            "create_ticket",
            {
                "title": "工具测试：支付接口大量 502",
                "description": "由 test_tools.py 创建",
                "category": "payment",
                "priority": "urgent",
            },
            admin_ctx,
        )
        chk("创建成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:300])
        new_no = r.get("ticket_no")
        chk("返回了工单号", bool(new_no), str(r))
        chk("★ SLA 截止时间已自动计算", bool(r.get("sla_deadline")), str(r))
        print(f"        {DIM}{json.dumps(r, ensure_ascii=False)}{RESET}")

        # 非法参数
        r = await call(
            "create_ticket", {"title": "测试", "category": "不存在的分类"}, admin_ctx
        )
        chk("★ 非法 category 被拦住（返回错误而不是抛异常）", r.get("ok") is False, str(r)[:200])
        chk("错误信息是人话（能回灌给模型）", "category" in str(r.get("error", "")), str(r)[:200])

        # ══════════════════════════════════════════════════
        print("\n[9] 写工具：get_ticket / summarize_ticket")
        # ══════════════════════════════════════════════════
        if new_no:
            r = await call("get_ticket", {"ticket_no": new_no}, admin_ctx)
            chk("按工单号查详情成功", r.get("ok") is True, str(r)[:200])
            chk("详情含 sla_status", r.get("ticket", {}).get("sla_status") is not None)

            r = await call("summarize_ticket", {"ticket_no": new_no}, admin_ctx)
            chk("总结材料返回成功", r.get("ok") is True, str(r)[:200])
            chk("★ 含 timeline（评论+操作记录合并）", isinstance(r.get("timeline"), list))
            print(f"        {DIM}timeline 长度: {len(r.get('timeline', []))}{RESET}")

        r = await call("get_ticket", {"ticket_no": "OPS-999999-999999"}, admin_ctx)
        chk("★ 不存在的工单号返回错误而不是崩溃", r.get("ok") is False, str(r)[:200])

        # ══════════════════════════════════════════════════
        print("\n[10] 写工具：assign / status / comment")
        # ══════════════════════════════════════════════════
        if new_no:
            r = await call(
                "assign_ticket", {"ticket_no": new_no, "department_id": tech_id}, admin_ctx
            )
            chk("分派给技术部成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:200])
            chk("★ 部门名称已回填", r.get("department") == "技术部", str(r))
            print(f"        {DIM}{json.dumps(r, ensure_ascii=False)}{RESET}")

            # ⚠️ 注意：assign_ticket 会把工单自动从 pending 推进到 processing，
            # 所以这里不需要（也不能）再改成 processing —— 会命中"已经是该状态"的保护。
            r = await call(
                "change_ticket_status", {"ticket_no": new_no, "status": "processing"}, admin_ctx
            )
            chk(
                "★ 改成相同状态被拒绝（幂等保护）",
                r.get("ok") is False and "已经是该状态" in str(r.get("error", "")),
                str(r)[:200],
            )

            r = await call(
                "change_ticket_status", {"ticket_no": new_no, "status": "waiting"}, admin_ctx
            )
            chk("processing → waiting 允许", r.get("ok") is True, str(r)[:200])

            # ★ 真正的非法流转：waiting 只能去 processing / closed
            r = await call(
                "change_ticket_status", {"ticket_no": new_no, "status": "resolved"}, admin_ctx
            )
            chk("★ waiting → resolved 非法，被状态机拒绝", r.get("ok") is False, str(r)[:200])

            r = await call(
                "change_ticket_status", {"ticket_no": new_no, "status": "processing"}, admin_ctx
            )
            chk("waiting → processing 允许", r.get("ok") is True, str(r)[:200])

            r = await call(
                "change_ticket_status", {"ticket_no": new_no, "status": "resolved"}, admin_ctx
            )
            chk("processing → resolved 允许", r.get("ok") is True, str(r)[:200])

            r = await call(
                "add_ticket_comment", {"ticket_no": new_no, "content": "工具测试评论"}, admin_ctx
            )
            chk("添加评论成功", r.get("ok") is True, str(r)[:200])

        # ══════════════════════════════════════════════════
        print("\n[11] ★ 风险分级策略（policy）")
        # ══════════════════════════════════════════════════
        cases = [
            ("list_tickets", {}, False, "只读"),
            ("get_ticket", {"ticket_no": "X"}, False, "只读"),
            ("create_ticket", {"title": "x"}, False, "新增可撤销"),
            ("add_ticket_comment", {"content": "x"}, False, "单条常规修改"),
            ("assign_ticket", {"department_id": 1}, False, "单条常规修改"),
            ("change_ticket_status", {"status": "processing"}, False, "普通状态变更"),
            ("change_ticket_status", {"status": "closed"}, True, "终态不可逆"),
            ("update_ticket", {"priority": "urgent"}, True, "升级优先级"),
            ("update_ticket", {"priority": "low"}, False, "降级优先级"),
            ("batch_update_tickets", {"ticket_ids": list(range(20))}, True, "批量操作"),
            ("从未登记的工具", {}, True, "未知工具默认最严格"),
        ]
        for tool_name, args, expect_approval, label in cases:
            before = {"priority": "medium"} if tool_name == "update_ticket" else None
            result = assess_risk(tool_name, args, before=before)
            chk(
                f"  {label}: {tool_name} → 需确认={result.requires_approval}",
                result.requires_approval == expect_approval,
                f"实际 {result.requires_approval} ({result.reason})",
            )

        # ══════════════════════════════════════════════════
        print("\n[12] ★ 越权防护：普通用户不能建工单给任意人 / 改他人工单")
        # ══════════════════════════════════════════════════
        # zhangsan 建一张自己的
        r = await call(
            "create_ticket", {"title": "zhangsan 自己的工单", "priority": "low"}, user_ctx
        )
        chk("普通用户可以创建工单", r.get("ok") is True, str(r)[:200])
        own_no = r.get("ticket_no")

        # admin 建一张，zhangsan 去改
        r = await call(
            "create_ticket", {"title": "admin 的工单", "priority": "low"}, admin_ctx
        )
        other_no = r.get("ticket_no")

        if other_no:
            r = await call(
                "update_ticket", {"ticket_no": other_no, "title": "我要改别人的"}, user_ctx
            )
            chk("★ 普通用户改他人工单被拒", r.get("ok") is False, str(r)[:250])
            print(f"        {DIM}{r.get('error', '')[:120]}{RESET}")

        if own_no:
            r = await call(
                "update_ticket", {"ticket_no": own_no, "title": "改自己的"}, user_ctx
            )
            chk("普通用户改自己的工单允许", r.get("ok") is True, str(r)[:200])

    print(f"\n{'=' * 50}")
    if failed == 0:
        print(f"{GREEN}全部通过{RESET}  {passed}/{passed + failed}")
    else:
        print(f"{RED}失败 {failed}{RESET} / 共 {passed + failed}   {GREEN}通过 {passed}{RESET}")
    print("=" * 50)
    return 1 if failed else 0


async def _run() -> int:
    try:
        return await main()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
