"""工单接口端到端测试。

跑之前先启动服务并灌入种子数据：
    uvicorn app.main:app --port 8000
    python scripts/seed.py

然后：
    python scripts/test_tickets.py
"""

from __future__ import annotations

import argparse
import json
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
            print(f"        {DIM}{str(detail)[:400]}{RESET}")


def show(label: str, data) -> None:
    text = json.dumps(data, ensure_ascii=False)
    print(f"        {DIM}{label}: {text[:280]}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    api = f"{args.base_url.rstrip('/')}/api/v1"

    with httpx.Client(base_url=api, timeout=30.0, trust_env=False) as c:
        # ── 登录两种角色 ───────────────────────────────────
        admin_r = c.post("/auth/login", json={"username": "admin", "password": "admin123456"})
        if admin_r.status_code != 200:
            print(f"{RED}admin 登录失败，请先跑 scripts/seed.py{RESET}")
            print(admin_r.text[:300])
            return 1
        admin_h = {"Authorization": f"Bearer {admin_r.json()['access_token']}"}

        user_r = c.post("/auth/login", json={"username": "zhangsan", "password": "ops123456"})
        user_h = {"Authorization": f"Bearer {user_r.json()['access_token']}"}

        # ── 1. 创建工单 ────────────────────────────────────
        print("\n[1] 创建工单 POST /tickets")
        r = c.post(
            "/tickets",
            json={
                "title": "测试：支付接口 502",
                "description": "自动化测试创建的工单",
                "category": "payment",
                "priority": "urgent",
            },
            headers=admin_h,
        )
        chk("返回 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        ticket = r.json() if r.status_code == 201 else {}
        if ticket:
            show("响应", ticket)
            chk("自动生成工单号", ticket.get("ticket_no", "").startswith("OPS-"), ticket.get("ticket_no"))
            chk("初始状态是 pending", ticket.get("status") == "pending")
            chk("★ SLA 截止时间已自动计算", ticket.get("sla_deadline") is not None)
            chk("★ 时间带 Z 后缀(UTC 标记)", str(ticket.get("created_at", "")).endswith("Z"), ticket.get("created_at"))
            chk("带上了创建人名字", bool(ticket.get("creator_name")))

        tid = ticket.get("id")

        # ── 2. 创建时的参数校验 ─────────────────────────────
        print("\n[2] 参数校验")
        r = c.post("/tickets", json={"title": "x"}, headers=admin_h)
        chk("标题太短返回 422", r.status_code == 422, str(r.status_code))

        r = c.post(
            "/tickets",
            json={"title": "测试工单", "assignee_id": 999999},
            headers=admin_h,
        )
        chk("负责人不存在返回 400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")
        if r.status_code == 400:
            chk("错误码是 BAD_REQUEST", r.json().get("code") == "BAD_REQUEST")

        # ── 3. 状态机 ──────────────────────────────────────
        print("\n[3] 状态机校验（核心）")
        if tid:
            # pending → resolved 是非法流转（必须先分派处理）
            r = c.patch(f"/tickets/{tid}/status", json={"status": "resolved"}, headers=admin_h)
            chk("pending → resolved 被拒绝(409)", r.status_code == 409, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 409:
                body = r.json()
                show("响应", body)
                chk("错误码是 CONFLICT", body.get("code") == "CONFLICT")
                chk("★ 告诉了你允许流转到哪些状态", "allowed" in (body.get("detail") or {}))

            # pending → processing 合法
            r = c.patch(f"/tickets/{tid}/status", json={"status": "processing"}, headers=admin_h)
            chk("pending → processing 成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

            # 改成相同状态
            r = c.patch(f"/tickets/{tid}/status", json={"status": "processing"}, headers=admin_h)
            chk("改成相同状态返回 400", r.status_code == 400, str(r.status_code))

        # ── 4. 分派 ────────────────────────────────────────
        print("\n[4] 分派 PATCH /tickets/{id}/assign")
        # 找一个真实用户 id
        me = c.get("/auth/me", headers=user_h).json()
        if tid:
            r = c.patch(
                f"/tickets/{tid}/assign",
                json={"assignee_id": me["id"]},
                headers=admin_h,
            )
            chk("分派成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 200:
                show("响应", {"assignee_name": r.json().get("assignee_name"), "status": r.json().get("status")})
                chk("assignee_name 已填充", bool(r.json().get("assignee_name")))

            r = c.patch(f"/tickets/{tid}/assign", json={}, headers=admin_h)
            chk("两个字段都不传返回 422", r.status_code == 422, str(r.status_code))

        # ── 5. 改优先级 → 重算 SLA ─────────────────────────
        print("\n[5] 改优先级自动重算 SLA")
        if tid:
            before = c.get(f"/tickets/{tid}", headers=admin_h).json().get("sla_deadline")
            r = c.put(f"/tickets/{tid}", json={"priority": "low"}, headers=admin_h)
            chk("修改成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            after = r.json().get("sla_deadline") if r.status_code == 200 else None
            chk("★ 优先级降低后 SLA 截止时间被推后", before != after, f"{before} → {after}")

        # ── 6. 评论 ────────────────────────────────────────
        print("\n[6] 评论")
        if tid:
            r = c.post(f"/tickets/{tid}/comments", json={"content": "自动化测试评论"}, headers=user_h)
            chk("发表评论成功", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 201:
                chk("评论人名字已填充", bool(r.json().get("author_name")))

        # ── 7. 详情 ────────────────────────────────────────
        print("\n[7] 工单详情")
        if tid:
            r = c.get(f"/tickets/{tid}", headers=admin_h)
            chk("返回 200", r.status_code == 200, str(r.status_code))
            if r.status_code == 200:
                d = r.json()
                chk("带 comments", isinstance(d.get("comments"), list) and len(d["comments"]) > 0)
                chk("★ 带 events 操作记录", isinstance(d.get("events"), list) and len(d["events"]) > 0)
                chk("★ 含 SLA 状态字段", d.get("sla_status") is not None, d.get("sla_status"))
                types = [e["event_type"] for e in d.get("events", [])]
                show("事件类型", types)
                chk("created 事件已记录", "created" in types)
                chk("status_changed 事件已记录", "status_changed" in types)
                chk("priority_changed 事件已记录", "priority_changed" in types)

        # ── 8. 列表与筛选 ──────────────────────────────────
        print("\n[8] 列表与筛选")
        r = c.get("/tickets", headers=admin_h)
        chk("列表返回 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            body = r.json()
            chk("分页字段完整", set(body) == {"items", "total", "page", "page_size"}, str(list(body.keys())))
            chk("有数据", body["total"] > 0, f"total={body['total']}")
            print(f"        {DIM}总数 {body['total']} 条{RESET}")

        r = c.get("/tickets", params={"status": "pending", "page_size": 5}, headers=admin_h)
        chk("按状态筛选生效", r.status_code == 200 and all(i["status"] == "pending" for i in r.json()["items"]), str(r.status_code))

        r = c.get("/tickets", params={"priority": "urgent", "priority": "high"}, headers=admin_h)
        chk("多值筛选生效", r.status_code == 200 and all(i["priority"] in ("urgent", "high") for i in r.json()["items"]))

        r = c.get("/tickets", params={"page": 1, "page_size": 5}, headers=admin_h)
        p1 = [i["id"] for i in r.json()["items"]]
        r = c.get("/tickets", params={"page": 2, "page_size": 5}, headers=admin_h)
        p2 = [i["id"] for i in r.json()["items"]]
        chk("★ 分页不重复", not set(p1) & set(p2), f"p1={p1} p2={p2}")

        # ── 9. 场景 4 的关键筛选 ───────────────────────────
        print("\n[9] 场景 4 筛选：超过 24 小时未处理")
        r = c.get("/tickets", params={"unhandled_hours": 24, "page_size": 100}, headers=admin_h)
        chk("返回 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            items = r.json()["items"]
            print(f"        {DIM}找到 {r.json()['total']} 条{RESET}")
            chk("★ 数量足够做场景 4 演示(>=10)", r.json()["total"] >= 10, f"只有 {r.json()['total']} 条")
            chk(
                "★ 全部是 pending 状态（不该混入已处理的）",
                all(i["status"] == "pending" for i in items),
                str({i["status"] for i in items}),
            )

        # ── 10. 统计 ───────────────────────────────────────
        print("\n[10] 统计 GET /tickets/statistics")
        r = c.get("/tickets/statistics", headers=admin_h)
        chk("返回 200（路由顺序正确，没被 /{id} 抢走）", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        if r.status_code == 200:
            s = r.json()
            show("统计", s)
            chk("by_status 有数据", bool(s.get("by_status")))
            chk("by_priority 有数据", bool(s.get("by_priority")))
            chk("total = 各状态之和", s["total"] == sum(s["by_status"].values()))

        # ── 11. 数据权限 ───────────────────────────────────
        print("\n[11] 数据权限（user 只能看自己的）")
        r_user = c.get("/tickets", params={"page_size": 100}, headers=user_h)
        r_admin = c.get("/tickets", params={"page_size": 100}, headers=admin_h)
        chk("user 能看到列表", r_user.status_code == 200, str(r_user.status_code))
        if r_user.status_code == 200 and r_admin.status_code == 200:
            u_total = r_user.json()["total"]
            a_total = r_admin.json()["total"]
            print(f"        {DIM}user 可见 {u_total} 条 / admin 可见 {a_total} 条{RESET}")
            chk("★ user 看到的数据比 admin 少", u_total < a_total, f"{u_total} vs {a_total}")

        # ── 12. 越权操作 ───────────────────────────────────
        print("\n[12] 越权防护")
        #
        # ⚠️ 注意：不能拿前面那个 tid 来测。
        # 它在第 4 步已经被分派给 zhangsan 了，zhangsan 作为负责人
        # 本来就【有】权限改它 —— 那样测出来会是 200，但代码其实是对的。
        # 这里新建一张 admin 创建、没有负责人的工单，才测得出越权。
        r = c.post(
            "/tickets",
            json={"title": "测试越权用的工单", "priority": "low"},
            headers=admin_h,
        )
        other_tid = r.json().get("id") if r.status_code == 201 else None

        if other_tid:
            r = c.put(
                f"/tickets/{other_tid}", json={"title": "我要改别人的工单"}, headers=user_h
            )
            chk("★ 普通用户改他人工单返回 403", r.status_code == 403, f"{r.status_code} {r.text[:200]}")
            if r.status_code == 403:
                chk("错误码是 FORBIDDEN", r.json().get("code") == "FORBIDDEN")

            r = c.patch(
                f"/tickets/{other_tid}/status", json={"status": "processing"}, headers=user_h
            )
            chk("★ 普通用户改他人工单状态返回 403", r.status_code == 403, str(r.status_code))

            r = c.delete(f"/tickets/{other_tid}", headers=user_h)
            chk("★ 普通用户删除工单返回 403", r.status_code == 403, str(r.status_code))

        # 但 user 改自己创建的工单应该是允许的
        r = c.post(
            "/tickets",
            json={"title": "zhangsan 自己的工单", "priority": "low"},
            headers=user_h,
        )
        own_tid = r.json().get("id") if r.status_code == 201 else None
        if own_tid:
            r = c.put(
                f"/tickets/{own_tid}", json={"title": "改自己的工单"}, headers=user_h
            )
            chk("普通用户改自己创建的工单应当允许", r.status_code == 200, str(r.status_code))

        # ── 13. 未登录 ─────────────────────────────────────
        print("\n[13] 未登录访问")
        r = c.get("/tickets")
        chk("返回 401", r.status_code == 401, str(r.status_code))

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
