# 运行、演示与可靠性验证

这份文档提供项目的检查路径。它描述个人项目的实现与验证范围，不把模拟数据、历史脚本记录或 SQLite 回归当成真实企业运行和 MySQL 生产环境的成绩。

## 运行前提

- 全栈演示需要 Docker 与 Docker Compose；本地开发的另一条路径见 [README 快速开始](../README.md#四快速开始)。
- 基础工单与看板不调用模型。Agent 演示需要在根目录 `.env` 配置可用的 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`，模型应支持工具调用与结构化输出。
- 种子数据和集成脚本会写入数据库，使用隔离的演示库。示例账号、数据库密码和 JWT 密钥属于本地演示配置。

在仓库根目录执行：

```bash
cp .env.example .env
# 编辑 .env 中的模型配置
docker compose up -d --build
docker compose ps
```

PowerShell 的复制命令是 `Copy-Item .env.example .env`。后端启动时执行 Alembic 迁移；待服务就绪后生成演示数据：

```bash
docker compose exec backend python scripts/seed.py
```

打开 [界面](http://127.0.0.1:8080)，使用 `admin / admin123456` 登录。可分别检查 [进程存活](http://127.0.0.1:8080/api/v1/health)、[数据库](http://127.0.0.1:8080/api/v1/health/db) 与 [Redis](http://127.0.0.1:8080/api/v1/health/redis)。进程存活返回成功不意味着数据库、Redis 或模型可用。

```bash
docker compose logs --tail 100 backend frontend
```

Compose 与 Nginx 配置可以作为 Linux 容器部署的起点；仓库没有提供 AWS EC2 实际上线、负载测试或多副本恢复验证记录。

## 演示顺序与观察点

建议先核查业务数据，再体验 Agent，最后演示审批边界。具体模型输出与工单数量取决于当时的种子数据和对话，不以文档中的示例数字为验收结果。

| 步骤 | 操作 | 观察点 |
|---|---|---|
| 1. 业务基线 | 查看工单列表、某条详情、SLA 与看板 | 能通过界面核对 Agent 使用的数据；记录候选工单原优先级和部门 |
| 2. 只读查询 | 打开右下角 AI 助手，输入“查询超过 24 小时未处理的工单，列出工单号、ID 和当前优先级” | 工具返回真实 ID；检查候选数量、截断标记与页面记录 |
| 3. 生成批量计划 | 输入“把刚才查询结果中前 3 条工单的优先级调整为 high，先给我看计划” | 出现待确认卡片；工单尚未发生这次批量变更，审批/运行记录已写入 |
| 4. 缩小范围 | 核对卡片，在原集合内取消一条，再确认 | 只处理保留的工单；在列表、详情操作记录和结果汇总交叉核查 |
| 5. 拒绝 | 重新生成一个待审计划并点击拒绝 | 计划被拒绝，该计划对应的工单变更没有执行 |
| 6. 查询运行 | 使用 `/api/v1/agent/runs/{run_id}` 查看运行与步骤 | 检查工具与运行记录；前端历史回放 UI 尚未实现 |

如果查询返回空集合，先核查种子数据是否存在；如果返回了截断标记，不应将当前候选解释为“全部”。可改用更小的查询范围继续演示。

审批接口返回 SSE。连接建立后，重复决定、过期或参数错误等业务失败可能出现在 `error` 事件中，**不能仅凭 HTTP 200 判断成功，也不应将此类失败统一描述成 HTTP 409**。当前 SSE 可用于观察阶段与最终结果，不将每条工单都实时推送进度作为已完成能力。

## 自动回归：不依赖外部服务

在 `backend/` 下安装依赖并运行：

```bash
python -m pip install -r requirements.txt
python -m pytest tests_offline
```

主要用例位于 [test_approval_safety.py](../backend/tests_offline/test_approval_safety.py)。测试使用临时 SQLite、独立数据库 Session 和构造的用户/审批记录；不启动 API 服务，不连接既有 MySQL 或 Redis，不请求模型。

检查范围包含：

- 两个确认请求竞争同一个 `pending` 计划时，仅一个决定成功；确认与拒绝竞争同理。
- 已决定或已过期的计划不能再次被决定；失败的确认参数不能消耗有效的待审计划。
- 后端身份与有效性检查，防止无权用户决定他人的计划。
- 传入确认范围时，只允许原计划工单 ID 的非空子集；拒绝范围扩张、重复、非法类型、超限及额外字段。

本次改动的开发环境已执行上述离线套件，结果为 **37 passed**（2026-10-28）；后续以对应提交的终端或 CI 日志为准。SQLite 用例验证条件更新和输入约束，并检查执行前身份刷新，不能证明 MySQL 锁等待、死锁、网络分区或真实多副本部署下的表现；它也不覆盖 LLM 是否选择了正确业务目标。

## 需要服务的集成检查

以下脚本保留在 `backend/scripts/`，与离线回归分开运行。历史 README 曾记录 259 项集成断言，该历史数量不代表本次复跑通过，也不与 pytest 用例合并统计。

### API、数据库与缓存

先按 README 本地开发路径启动 MySQL、Redis 和后端，初始化演示数据，再从 `backend/` 执行：

```bash
python scripts/smoke_test.py
python scripts/test_tickets.py
python scripts/test_tools.py
python scripts/test_logout.py
python scripts/test_cache.py
```

这些脚本检查不同层次的接口、权限、SLA、工具和缓存行为，不是互相替代的完整覆盖。`test_tools.py` 无需模型，但仍需要数据库与种子用户；缓存和登出检查需要 Redis。

### 模型与前后端链路

```bash
python scripts/test_llm.py
python scripts/test_agent.py --only batch
```

这些命令真实请求模型，Agent 场景会修改演示工单。额外运行 `python scripts/verify_ui.py` 时，需要前端 Vite 开发服务运行在 `127.0.0.1:5173`，它不能直接代替对 Compose 中 Nginx 代理的检查，也不等同于浏览器渲染测试。

手工全栈验收应记录使用的提交、环境、模型、命令与结果，重点检查：

1. 待审卡片出现后、确认之前，目标工单未发生该批量变更。
2. 缩小勾选范围后，未勾选的工单未被执行；拒绝路径同理。
3. 对同一 action 重复/并发确认时，读取 SSE 业务错误并核查实际工单操作记录。
4. 已关闭或不存在的工单等单条失败，是否进入失败清单；成功部分是否随外层事务提交。
5. 经 Nginx 访问时，阶段事件是否在整个流结束前到达；核查 `proxy_buffering off`，而非仅看页面是否最终出现文字。

本文提供验收步骤，不把尚未运行的真实服务检查标记为通过。

## 当前保证与边界

| 范围 | 当前实现 | 不包含的保证 |
|---|---|---|
| 审批决定 | 通过 `UPDATE ... WHERE status = pending AND expires_at > now` 等条件抢占有效计划；只有一个请求能成功改变决定 | 不保证获准执行的任务在进程崩溃后自动恢复 |
| 确认集合 | 只能缩小原工单 ID 集合；不允许在确认时新增业务参数 | 不保证模型生成的原计划必然符合用户真实意图，仍需核对 |
| 计划持久化 | 待审计划存 MySQL，进程重启后记录仍可查询；是否仍可确认取决于有效期与状态 | 不等同于恢复已经开始或部分完成的执行 |
| 部分失败 | SAVEPOINT 隔离单条异常，成功部分在外层提交 | 不等同于每条独立持久化；进程故障和提交失败需单独处理 |
| 执行结果 | 审批决定、工单变更与结果记录是分开的步骤 | 不保证跨这些步骤的 exactly-once；可能出现 `approved` 悬挂，或业务已提交而结果未记完 |
| 工具策略 | 后端身份注入、Service 规则及意图工具白名单 | 不保证提示词能完全消除误选工具或参数错误 |
| 审批执行器 | 当前支持 `batch_update_tickets` 与 `batch_assign_tickets` | 策略标记的其他单条高风险操作仍需完善执行映射与端到端验证 |
| Redis 失败 | 统计缓存可回源；JWT 黑名单采用 fail-open | Redis 不可用时，已登出 token 的撤销检查会降级，不能宣称此时立即失效 |
| SSE | 阶段事件、结果汇总、心跳与代理防缓冲配置 | 不保证网络断开后的自动续传、前端完整历史回放或逐条实时执行进度 |

对 `approved` 悬挂或结果缺失的操作，应先人工核对计划、工单当前值与操作记录，不能自动把状态改回 `pending` 后重放。后续如需恢复，应设计执行状态、业务幂等键与可核查的重试/补偿流程，再做故障注入验证。

## 可讨论的故障案例

这些是开发问题记录，适合结合源码解释排查方法；它们不是外部客户的线上事故经历。

| 现象 | 根因与修复 | 核查位置 |
|---|---|---|
| 批量计划需要 ID，但查询结果只给工单展示字段 | 工具之间的数据契约不完整；查询结果补充真实 ID，不让模型猜测 | `backend/app/agent/tools/read_tools.py`、`nodes.py` 与 README 问题记录 |
| “所有工单”静默遗漏 | 查询存在 limit；返回总数与截断标记，先核对范围，再生成计划 | 工单查询工具、提示词和待审预览 |
| 预览生成失败但查询本身正常 | 把为控制上下文而截断的 JSON 再当结构化数据解析；改为独立保存完整 `observations` | `backend/app/agent/nodes.py` |
| 先读取 `pending` 再更新导致并发请求都通过 | 判断和更新分开存在竞争窗口；改用数据库条件更新，并用独立 Session 并发回归 | `backend/app/services/agent_service.py`、`backend/tests_offline/` |
| 本地可启动、全新容器缺依赖 | 开发环境手工安装过的包未进入依赖清单；补依赖并保留检查脚本 | `backend/requirements.txt`、`backend/scripts/check_deps.py` |

回到 [README](../README.md)。
