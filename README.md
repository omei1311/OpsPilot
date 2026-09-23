# OpsPilot · 企业智能工单与运营协同平台

> 一个**能真正操作业务数据**的 AI Agent 系统 —— 不是聊天机器人，也不是 Todo List。
>
> 它首先是一套完整的企业工单系统（工单流转 / 状态机 / SLA / 审计 / 数据看板），
> 在此之上内置一个 Copilot：理解自然语言、调用业务工具、SSE 流式反馈执行过程，
> 并在**改动数据之前必须拿到人工确认**。

**技术关键词**：LangGraph · Tool Calling · Structured Output · Human-in-the-loop · SSE 流式 · FastAPI 异步 · SQLAlchemy 2.x · Vue 3 + TypeScript · Docker Compose

**项目规模**：后端 52 个 Python 文件 · 前端 25 个 Vue/TS 文件 · 11 张数据库表 · 13 个 Agent 工具 · **259 项可执行测试断言**

---

## 目录

- [一、这个项目解决什么问题](#一这个项目解决什么问题)
- [二、功能清单](#二功能清单)
- [三、系统架构](#三系统架构)
- [四、快速开始](#四快速开始)
- [五、四个核心 Agent 场景](#五四个核心-agent-场景)
- [六、Agent 架构详解](#六agent-架构详解)
- [七、SSE 事件设计](#七sse-事件设计)
- [八、Human-in-the-loop 详解](#八human-in-the-loop-详解)
- [九、数据库设计](#九数据库设计)
- [十、API 接口清单](#十api-接口清单)
- [十一、项目结构](#十一项目结构)
- [十二、测试与验证](#十二测试与验证)
- [十三、开发过程中解决的真实问题](#十三开发过程中解决的真实问题)
- [十四、技术决策记录](#十四技术决策记录)
- [十五、环境变量](#十五环境变量)
- [十六、常见问题](#十六常见问题)
- [十七、已知不足与后续规划](#十七已知不足与后续规划)

---

## 一、这个项目解决什么问题

传统工单系统的痛点是**"人适应系统"**：必须点开表单、逐字段填写、逐条筛选、逐条改状态。

OpsPilot 让**"系统适应人"**：用户用自然语言表达意图，Agent 负责翻译成结构化业务操作。而它的独特之处在于 —— **Agent 不是绕过业务系统的旁路，而是业务系统的第二个客户端**：

```
       人工点按钮              对 AI 说话
           │                     │
       REST API             Agent Tools
           └──────────┬──────────┘
                      ▼
               services/       ← 权限 / 状态机 / 事务 / 审计
                      ▼
                MySQL + Redis
```

Agent 工具**不允许直接碰数据库**，只能调 Service 层。这保证了「人改的数据」和「AI 改的数据」在权限校验、状态流转、审计日志上完全等价 —— 不需要额外维护两套逻辑。

## 二、功能清单

### 2.1 用户与权限

| 功能 | 说明 |
|---|---|
| 注册 / 登录 | bcrypt 密码哈希（cost=12）、JWT（含 jti）、支持用户名或邮箱登录 |
| 登出 | JWT 黑名单（Redis），登出后 token **立即失效**，不是只删本地存储 |
| 角色模型 | `admin` / `operator` / `user` 三级角色 |
| 数据权限 | 普通用户只能看到自己创建或被分派的工单，在 Service 层强制生效、不可绕过 |
| 防枚举 | 登录失败时"用户不存在"和"密码错误"返回完全相同的提示 |

### 2.2 工单中心

| 功能 | 说明 |
|---|---|
| 工单 CRUD | 创建 / 列表 / 详情 / 修改 / 删除（仅 admin） |
| 工单号 | `OPS-YYYYMM-000123` 格式，基于自增 ID 生成，天然唯一无并发冲突 |
| 状态机 | `pending → processing → waiting → resolved → closed`，非法流转返回 409 并告知允许的目标 |
| 分派 / 转派 | 可派给部门或个人，分派后自动从 pending 推进到 processing |
| 优先级 | `low / medium / high / urgent`，修改后**自动重算 SLA 截止时间** |
| 评论 | 时间正序展示，同样写入操作记录 |
| 操作记录 | `ticket_events` 只增不改，每次创建/分派/改状态/改优先级/评论都留痕 |
| 筛选 | 状态 / 优先级 / 分类多选 + 关键词模糊搜索 + **「超过 N 小时未处理」**快捷筛选 |
| 分页 | offset/limit 分页，`created_at + id` 双键排序保证翻页不重不漏 |

### 2.3 SLA 管理

| 功能 | 说明 |
|---|---|
| SLA 策略 | 按优先级配置解决时限（urgent 4h → low 48h），存 `sla_policies` 表 |
| 截止时间 | 创建工单时自动计算；改优先级时**从创建时刻**重算（不是从当前时刻） |
| 风险状态 | `normal / at_risk / overdue / done`，"at_risk" = 剩余时间不足总时限 20% |
| 兜底策略 | 策略表为空时使用代码内置默认值，保证工单永远能算出截止时间 |

### 2.4 数据看板（Dashboard）

| 功能 | 说明 |
|---|---|
| 概览卡片 | 总数 / 今日新增 / 今日解决 / 待处理 / 处理中 / 已解决 / SLA 超时 |
| 趋势折线图 | 每天"新增 vs 解决"，支持 7/14/30 天切换，**自动补齐无数据日期** |
| 分布饼图 | 按状态 / 优先级 / 分类 |
| SLA 风险清单 | 最紧急的 N 条工单，点击跳转详情 |
| 处理人工作量 | 堆叠柱状图（处理中 / 已解决） |
| 统计口径 | 与 Agent 的 `analyze_sla_risk` 工具共用同一个 Service —— AI 说的和页面显示的永远一致 |
| **统计缓存** | 五个统计接口走 Redis Cache-Aside（60s TTL + 写操作主动失效）；**缓存 key 带权限维度**（`all`/`u{用户id}`），防止跨权限的缓存投毒；Redis 故障 fail-open 回源查库 |

### 2.5 Agent Copilot

| 功能 | 说明 |
|---|---|
| 意图识别 | 6 类意图（创建/查询/分析/批量/修改单条/闲聊），Structured Output 强约束 |
| 工具调用 | 13 个业务工具，ReAct 循环，支持多轮连续调用 |
| 多轮对话 | 会话历史存库，注入最近 10 条作为上下文 |
| 流式反馈 | SSE 实时推送：意图 → 思考 → 工具调用卡片 → 打字机回答 → 引用工单 |
| 执行过程回放 | `agent_run_steps` 落库，刷新页面后可通过 run_id 完整回放 |
| 防幻觉 | 回答中引用的工单号全部来自真实查询结果，前端渲染成可点击标签 |
| 循环保护 | 最大 12 步 + 单次 50k token 上限 + 120s 超时，三重保险 |
| 成本可观测 | `agent_runs` 记录每次运行的 token 消耗 / 工具调用次数 / 耗时 |

### 2.6 Human-in-the-loop

| 功能 | 说明 |
|---|---|
| 强制确认 | 批量操作（`batch_update_tickets` / `batch_assign_tickets`）无条件需要人工确认 |
| 确认卡片 | 列出**具体工单号和标题**（不是数字），支持逐条取消勾选 |
| 倒计时 | 10 分钟有效期，过期自动作废 |
| 幂等保护 | 重复点确认返回 409，不会执行两遍 |
| 部分成功 | SAVEPOINT 实现逐条独立提交，17 条里 1 条失败不影响其余 16 条 |
| 审计闭环 | 工单的 `ticket_events` 记录 `batch: true` 标记，可回溯到是哪次 AI 运行、谁批准的 |

## 三、系统架构

### 3.1 整体架构

```
                         Browser
                            │
                     Vue3 + TypeScript
                     (Element Plus / ECharts / Pinia)
                            │
                    HTTP / SSE (fetch)
                            │
                            ▼
                    ┌───────────────┐
                    │ nginx (前端容器) │  静态资源 + API 反代
                    │  ★ SSE 专用配置  │  proxy_buffering off
                    └───────┬───────┘
                            ▼
                    ┌───────────────┐
                    │ FastAPI 后端   │
                    │  api/v1/*     │  路由层：只转手
                    │  services/    │  业务层：权限/状态机/事务
                    │  agent/       │  AI 层：LangGraph 状态图
                    │    └─ tools/  │  13 个工具（只调 Service）
                    └──┬────────┬──┘
                       │        │
                  ┌────▼──┐ ┌───▼───┐
                  │ MySQL │ │ Redis │  (JWT 黑名单)
                  └───────┘ └───────┘
                            │
                            ▼ (出容器)
                     LLM API (OpenAI 兼容)
                     阿里云百炼 / DeepSeek / Ollama...
```

### 3.2 后端分层（严格单向依赖）

| 层 | 目录 | 职责 | 禁止 |
|---|---|---|---|
| API | `app/api/v1/` | 参数校验、鉴权依赖、调用 Service | 写 SQL、写业务规则 |
| Service | `app/services/` | 业务规则、状态机、事务边界、数据范围 | 感知 HTTP（不 import Request） |
| Model | `app/models/` | SQLAlchemy ORM 映射 | 写业务方法 |
| Schema | `app/schemas/` | Pydantic 出入参（白名单序列化） | 泄露 `password_hash` 等敏感字段 |
| Agent | `app/agent/` | 图编排、工具、提示词、风险策略 | **直接访问 ORM Session** |
| Core | `app/core/` | 配置、安全、异常、日志、Redis | — |

`api/` 和 `agent/` 是**两个平行入口**，互不依赖，共享同一个 Service 层。

### 3.3 技术栈与实际版本

| 层 | 技术 | 实际版本 |
|---|---|---|
| 后端语言 | Python | 3.12（开发）/ 3.11-slim（Docker） |
| Web 框架 | FastAPI + Uvicorn | 0.141 / 0.53 |
| ORM | SQLAlchemy 2.x（全异步） + Alembic | 2.0.54 / 1.20 |
| 校验 | Pydantic v2 + pydantic-settings | 2.13 |
| AI 编排 | LangGraph + langchain-core + langchain-openai | 1.2.11 / 1.6.3 / 1.6.2 |
| 认证 | PyJWT + bcrypt | 2.14 / 5.0 |
| 数据库 | MySQL 8.0（asyncmy 驱动） | 8.0.46 |
| 缓存 | redis-py（异步） | 8.1 |
| 前端 | Vue 3 + TypeScript + Vite | 3.5 / 5.6 / 6.4 |
| UI / 图表 | Element Plus + ECharts + Pinia + Vue Router | 2.9 / 6.1 |
| 部署 | Docker + Docker Compose + nginx | — |

> LLM 通过 OpenAI 兼容接口接入，默认配置为阿里云百炼 `qwen-plus`，改两个环境变量即可切换 DeepSeek / 智谱 / Kimi / 本地 Ollama。

## 四、快速开始

### 4.1 方式一：Docker Compose 全栈部署

**前置**：Docker Desktop 已启动。

```bash
# ① 准备环境变量
cp .env.example .env
#    编辑 .env，把 LLM_API_KEY 换成你自己的
#    申请地址：https://bailian.console.aliyun.com/（有免费额度）

# ② 构建并启动（4 个容器：mysql / redis / backend / frontend）
docker compose up -d --build
```

访问 **http://127.0.0.1:8080**

<details>
<summary><b>国内网络拉不动镜像？点开这里</b></summary>

Docker Hub 在国内经常连不上。本项目提供了镜像源覆盖文件：

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

它会通过 `--build-arg` 把基础镜像替换为 `dockerproxy.net` 等国内可达的镜像源。

更彻底的方案是配置 Docker daemon 的 `registry-mirrors`（一劳永逸）：

```json
// C:\Users\<你>\.docker\daemon.json
{
  "registry-mirrors": [
    "https://dockerproxy.net",
    "https://docker.1panel.live",
    "https://docker.m.daocloud.io"
  ]
}
```

改完重启 Docker Desktop 生效。
</details>

**首次启动说明**：

- 后端容器启动时会自动执行 `alembic upgrade head` 建表
- 需要手动灌一次演示数据（见 4.2 第②步的 `seed.py`，对容器执行：`docker exec opspilot-backend python scripts/seed.py`）
- 构建时如果报 `PACKAGES DO NOT MATCH THE HASHES`，是网络抖动导致包下载损坏，pip 哈希校验拦住了（这是保护），重跑一次 `build` 即可

### 4.2 方式二：本地开发（改代码热重载，推荐日常使用）

**前置**：Python 3.11+、Node.js 18+（建议 22）、Docker。

```bash
# ── ① 启动 MySQL + Redis（宿主机端口 3307 / 6380）──
docker compose -f docker-compose.dev.yml up -d

# ── ② 后端 ──
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux/macOS

pip install -r requirements.txt

copy .env.example .env            # 编辑：填 LLM_API_KEY，端口默认已对好 dev 容器
alembic upgrade head              # 建表
python scripts/seed.py            # 灌演示数据（60 工单/95 评论/165 操作记录）
python -m uvicorn app.main:app --reload --port 8000

# ── ③ 前端（另开一个终端）──
cd frontend
npm install
npm run dev
```

访问 **http://127.0.0.1:5173**（Vite 开发代理自动转发 `/api` 到 8000，无需 CORS 配置）。

### 4.3 演示账号

`python scripts/seed.py` 会创建以下账号：

| 用户名 | 密码 | 角色 | 能做什么 |
|---|---|---|---|
| `admin` | `admin123456` | 管理员 | 全部功能，含删除工单 |
| `operator` | `ops123456` | 操作员 | 创建/分派/批量操作 |
| `zhangsan` / `lisi` / `wangwu` | `ops123456` | 普通用户 | 只能看到自己的工单 |

### 4.4 种子数据说明

`scripts/seed.py` 刻意做了时间分布，保证每个演示场景都有素材：

| 数据 | 数量 | 用途 |
|---|---|---|
| 超过 24 小时未处理的 pending 工单 | 20 条 | **场景 4 的前提**，没有它们批量操作无单可改 |
| SLA 已超时的工单 | 7 条 | 场景 3 的分析素材 |
| 处理中 / 已解决 / 已关闭 | 15 / 10 / 8 条 | 让看板和列表不空 |
| 部门 | 4 个（技术/客服/运维/产品） | 场景 4 的"分配给技术部门" |
| SLA 策略 | 4 条（按优先级） | 截止时间计算 |

重置数据：`python scripts/seed.py --reset`（清空工单/评论/记录，保留用户）

## 五、四个核心 Agent 场景

登录后点右下角 **「AI 助手」** 悬浮按钮，直接输入：

### 场景 1 · 自然语言创建工单

```
输入：线上支付接口大量出现 502，今天下午开始一直有用户反馈支付失败，帮我报个故障。

Agent 执行链（前端实时可见）：
  ① 意图识别 → create_ticket（置信度 0.98）
  ② 实体抽取 → {title: "支付接口大量返回 502", category: "payment", priority: "urgent"}
  ③ 调用 create_ticket 工具
  ④ 返回：工单号 OPS-202609-000123，SLA 截止时间已按 urgent(4h) 自动计算
```

实测模型输出：`create_ticket({"title": "支付接口大量返回 502", "category": "payment", "priority": "urgent", ...})` —— 参数提取完全正确。

### 场景 2 · 查询工单

```
输入：帮我看看最近有哪些高优先级工单没处理

Agent 执行链：
  ① 意图 → query_ticket
  ② list_tickets(priority=[high, urgent], status=[pending])
  ③ 生成回答 + 引用工单列表（可点击跳转，防幻觉）
```

### 场景 3 · SLA 风险分析

```
输入：分析最近 7 天的 SLA 风险

Agent 执行链：
  ① 意图 → analyze
  ② analyze_sla_risk(days=7)（与前端看板共用同一 Service，口径一致）
  ③ 输出：已超时 N 条、24 小时内到期 M 条、最紧急清单及建议
```

### 场景 4 · 批量修改（Human-in-the-loop）★ 核心

```
输入：把所有超过 24 小时未处理的工单升级成高优先级，并分配给技术部门。

Agent 执行链：
  ① 意图 → batch_update
  ② list_tickets(unhandled_hours=24, limit=100)     ← 只读，直接执行，找到 22 条
  ③ list_departments()                              ← 把"技术部门"映射成 department_id
  ④ 模型请求 batch_update_tickets(...)              ← 高风险！被 guard 节点拦截
  ⑤ 生成执行计划 → 落库 agent_pending_actions → SSE 推送确认卡片
  ⑥ ⏸ 等待人工确认（10 分钟倒计时，服务重启也不丢）
  ⑦ 用户核对 22 条工单，取消勾选 2 条 → 点「确认执行」
  ⑧ 事务内批量执行，逐条 SSE 推送进度
  ⑨ 完成后：工单 priority=high、部门=技术部、状态=processing
     ticket_events 记录 batch:true 标记，可回溯到本次 AI 运行
```

**验证过的安全边界**：确认前数据零改动；重复提交被 409 拦截（幂等）；拒绝路径数据零改动；普通用户发起批量操作被权限层直接拒绝。

## 六、Agent 架构详解

### 6.1 状态图（LangGraph StateGraph）

```
START
  │
  ▼
understand ──(chat 意图)──────────────────┐
  │  意图识别 + 工具白名单                 │
  ▼                                      │
agent ◄─────────────┐                    │
  │ ReAct 决策        │                    │
  ├─(无工具调用)──────┼──────────────────→ respond ──→ END
  ├─(高风险批量)──→ guard ──→ END          │
  └─(普通工具)───→ tools ──→ 回到 agent    │
                                          ▼
                                    (生成回答)
```

**节点职责**：

| 节点 | 职责 | 关键点 |
|---|---|---|
| `understand` | 意图分类 + 实体抽取 | `with_structured_output` + `Literal` 枚举强约束；失败降级为 chat 而不是崩溃 |
| `agent` | ReAct：决定调什么工具 | 只绑定当前意图白名单内的工具 |
| `tools` | 执行工具 | 结果截断后回灌模型；结构化数据同时存入 observations |
| `guard` | 高风险拦截 → 生成计划 | **不执行任何写操作**，只落库 + 推确认卡片 |
| `respond` | 生成最终回答 | 收集引用工单（防幻觉）|

**循环保护（三重）**：`step_count >= 12` 强制收尾 · `total_tokens >= 50000` 中断 · 120 秒图级超时。

### 6.2 工具清单（13 个）

| 分类 | 工具 | 风险 | 需确认 |
|---|---|---|---|
| 读 | `list_tickets` 工单列表查询 | low | 否 |
| 读 | `get_ticket` 工单详情 | low | 否 |
| 读 | `get_ticket_statistics` 统计 | low | 否 |
| 读 | `analyze_sla_risk` SLA 风险分析 | low | 否 |
| 读 | `summarize_ticket` 工单脉络汇总 | low | 否 |
| 读 | `list_departments` 部门列表 | low | 否 |
| 写 | `create_ticket` 创建工单 | low | 否（新增可撤销）|
| 写 | `update_ticket` 修改单条 | medium | 升级优先级时是 |
| 写 | `assign_ticket` 分派 | medium | 否 |
| 写 | `change_ticket_status` 改状态 | medium | 进终态(closed)时是 |
| 写 | `add_ticket_comment` 评论 | medium | 否 |
| 批量 | `batch_update_tickets` 批量修改 | **high** | **✅ 强制** |
| 批量 | `batch_assign_tickets` 批量分派 | **high** | **✅ 强制** |

**注意**：不提供 `delete_ticket` 工具 —— Agent 永远不能物理删除数据。

### 6.3 安全模型：身份从后端注入，LLM 碰不到

```python
# ❌ 危险：user_id 是工具参数，模型可以猜错、被提示注入利用、或干脆编一个
async def list_tickets(user_id: int, status: str = None): ...

# ✅ 安全：身份在签名之外，通过 RunnableConfig 注入
async def list_tickets(status: str = None, config: RunnableConfig = None):
    ctx = ToolContext.from_config(config)   # ← 身份由后端注入
```

LangChain 自动把 `config` 参数从工具的 JSON Schema 里剔除 —— 模型既看不到它也无法提供它。**测试断言遍历全部 13 个工具，验证 schema 中不出现 `user_id` / `creator_id` / `ctx` / `config`。**

### 6.4 意图 → 工具白名单

| 意图 | 可用工具 |
|---|---|
| `chat` | （无，直接回答）|
| `create_ticket` | list_departments, create_ticket |
| `query_ticket` | list_tickets, get_ticket, summarize_ticket, list_departments |
| `analyze` | list_tickets, get_ticket_statistics, analyze_sla_risk, summarize_ticket |
| `batch_update` | list_tickets, list_departments, **batch_update_tickets, batch_assign_tickets** |
| `update_ticket` | get_ticket, list_tickets, list_departments, update_ticket, assign_ticket, change_ticket_status, add_ticket_comment |

未知意图返回**空列表**（fail-safe）—— 宁可让模型说"不确定"，也不让它在未知意图下拿到写工具。

## 七、SSE 事件设计

### 7.1 为什么手写解析器，不用浏览器 EventSource

| EventSource 的硬伤 | 本项目的应对 |
|---|---|
| 不支持自定义请求头 → **带不了 JWT** | `fetch + ReadableStream` |
| 只能 GET → 聊天内容进 URL（中文编码/超长/进日志） | POST JSON body |
| 不能中途取消 → 用户关面板后端还在烧 token | `AbortController` |

代价是约 140 行协议解析（`frontend/src/api/sse.ts`）。核心坑：**一个网络 chunk ≠ 一帧事件**，可能包含多帧或半帧，必须维护 buffer 按空行切分。

### 7.2 事件类型

| 事件 | 触发时机 | 前端表现 |
|---|---|---|
| `run_started` | 一次运行开始 | 创建占位消息 |
| `intent` | 意图识别完成 | 意图标签 |
| `thought` | 模型思考文本 | 灰色斜体（打字机）|
| `tool_call` | 工具调用前 | 工具卡片（加载态，可展开看参数）|
| `tool_result` | 工具返回 | 卡片转完成态 + 耗时 |
| `plan` | 生成执行计划 | **★ 确认卡片** |
| `awaiting_approval` | 图挂起 | 锁定输入框 + 倒计时 |
| `action_executed` | 批量执行中每条 | 进度 +1 |
| `action_finished` | 批量执行结束 | 成功/失败汇总 |
| `token` | 回答增量文本 | 打字机效果 |
| `citations` | 引用工单列表 | 可点击标签 |
| `message` | 消息落库 | 持久化消息 ID |
| `error` | 出错 | 错误提示（不泄露堆栈）|
| `done` | 结束（**必定发送**）| 恢复输入框 |

前端遇到未知事件名一律忽略不报错 —— 后端加新事件不会让老前端崩溃（前向兼容）。

### 7.3 传输可靠性

- **心跳**：每 15 秒发 `: ping` 注释行，防止 nginx/网关掐断空闲连接（用户看确认卡片可能几分钟）
- **防缓冲**：后端返回 `X-Accel-Buffering: no` + nginx 配置 `proxy_buffering off`，双保险
- **验证**：测试断言"首事件到达时间远早于流结束" —— 证明是真流式，不是攒完一次发

## 八、Human-in-the-loop 详解

### 8.1 两阶段设计

```
阶段一（graph 的 guard 节点）          阶段二（executor）
─────────────────────────           ─────────────────────
生成计划 → 写 MySQL → 结束图          用户点确认 → 读计划 → 真正执行
                                     ↑
                          中间可以隔几分钟，可以重启服务
```

**为什么不用 LangGraph 的 `interrupt()` + Redis Checkpointer？**

| | 两阶段（本项目） | interrupt + Checkpointer |
|---|---|---|
| 等待状态存哪 | MySQL 业务表 | Redis 序列化结构 |
| 服务重启 | 不受影响 | 内存版会丢 |
| 可查询/统计 | ✅ 普通业务表 | ❌ 需解析序列化数据 |
| 额外依赖 | 无 | 需要 Checkpointer |
| 上下文连续性 | 简化（参数完整存计划里）| 完整保留图内状态 |

核心判断：**「等待确认」是业务状态，放业务库比放缓存更合适**。

### 8.2 确认卡片设计原则

让用户**真正能核对**，而不是盲目点同意：

| 设计 | 为什么 |
|---|---|
| 列出**具体工单号和标题** | 数字没法核对，具体列表才能发现"这条不该改" |
| **逐条取消勾选** | 用户可能只想改一部分，结果通过 `edited_payload` 回传 |
| 数量变化时**二次确认** | "你取消了 2 条，实际将修改 20 条，确定吗？" |
| **倒计时明确** | 避免用户以为点了就永久等待 |
| 拒绝按钮同等显眼 | 不做诱导确认的暗黑模式 |

### 8.3 安全边界（全部有测试覆盖）

| 保护 | 实现 |
|---|---|
| 确认前零改动 | guard 节点不执行任何写操作 |
| 幂等 | 只有 `pending` 状态可被决定，重复提交 409 |
| 过期 | 超时自动作废，需重新发起 |
| 二次权限校验 | 执行时重新验证用户身份（防 TOCTOU：提交时有权限、执行时已降权）|
| 批量上限 | 单次最多 100 条（`AGENT_BATCH_LIMIT` 安全阀）|
| 部分成功 | SAVEPOINT 逐条独立，失败清单完整返回 |

## 九、数据库设计

### 9.1 表清单（11 张业务表）

| 表 | 用途 | 关键设计 |
|---|---|---|
| `users` | 用户 | bcrypt 哈希；role 用 String 而非 ENUM（改取值不用 ALTER TABLE）|
| `departments` | 部门 | 扁平结构，不做树形 |
| `tickets` | 工单主表 | 复合索引 `(status, priority)`、`(assignee_id, status)` |
| `ticket_comments` | 评论 | `(ticket_id, created_at)` 索引，时间正序 |
| `ticket_events` | 操作记录 | **只增不改**，审计用途 |
| `sla_policies` | SLA 策略 | 按优先级一条 |
| `agent_conversations` | Agent 会话 | 最后活跃时间排序 |
| `agent_messages` | 消息历史 | 只存 user/assistant，工具结果不混入（控制上下文体积）|
| `agent_runs` | 一次运行 | run_id 贯穿全链路；记录 token/耗时 |
| `agent_run_steps` | 执行明细 | **独立存表而非 JSON 字段**：支持刷新后回放、可聚合统计 |
| `agent_pending_actions` | 待确认操作 | HITL 持久化载体，含状态机 + 过期时间 |

### 9.2 关键设计决策

**① 为什么 `agent_run_steps` 单独存表？**
前端刷新后要回放执行过程（按 run_id 分页查询）；将来统计"哪个工具最慢"可直接 GROUP BY。塞 JSON 字段两件事都做不了。

**② 为什么 `ticket_events` 只增不改？**
它是审计日志。一旦可被普通接口篡改，审计就失去意义。这张表没有 update/delete 接口。

**③ 为什么时间统一存 UTC？**
跨时区一致性。所有 `DATETIME` 字段存 UTC，出参经 `UTCDatetime` 序列化器统一带 `Z` 后缀，前端 `new Date()` 自动转本地时区。（踩过的坑：不加 Z 后缀时北京时间会凭空多 8 小时）

**④ 软删除？**
不做。工单业务中"删除"用 `cancelled` 状态表达，物理删除会破坏审计链。

## 十、API 接口清单

统一前缀 `/api/v1`，认证方式 `Authorization: Bearer <jwt>`，错误响应统一为 `{code, message, detail}`。

### 认证 `/auth`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` | 注册（用户名/邮箱查重）|
| POST | `/auth/login` | 登录（用户名或邮箱）|
| GET | `/auth/me` | 当前用户信息 |
| POST | `/auth/logout` | 登出（JWT 进 Redis 黑名单）|

### 工单 `/tickets`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tickets` | 创建（自动算 SLA、生成工单号）|
| GET | `/tickets` | 列表（多条件筛选 + 分页 + 数据权限）|
| GET | `/tickets/statistics` | 统计（⚠️ 路由须在 `/{id}` 之前注册）|
| GET | `/tickets/{id}` | 详情（含评论 + 操作记录）|
| PUT | `/tickets/{id}` | 修改（改优先级自动重算 SLA）|
| PATCH | `/tickets/{id}/status` | 状态流转（状态机校验）|
| PATCH | `/tickets/{id}/assign` | 分派/转派 |
| DELETE | `/tickets/{id}` | 删除（仅 admin）|
| POST | `/tickets/{id}/comments` | 添加评论 |

### 看板 `/dashboard`

`/overview` 概览 · `/trend` 趋势 · `/distribution` 分布 · `/sla-risk` 风险清单 · `/workload` 工作量

### Agent `/agent`

| 方法 | 路径 | 说明 |
|---|---|---|
| **POST** | **`/agent/chat`** | **★ SSE 对话主入口** |
| **POST** | **`/agent/actions/{action_id}/approve`** | **★ 确认执行（SSE 流式逐条进度）** |
| POST | `/agent/actions/{action_id}/reject` | 拒绝（SSE）|
| GET | `/agent/actions/pending` | 我的待确认列表 |
| GET | `/agent/conversations` | 会话列表 |
| GET | `/agent/conversations/{id}/messages` | 会话消息历史 |
| GET | `/agent/runs/{run_id}` | 运行详情（含执行步骤，用于回放）|

### 健康检查 `/health`

`/health` 存活（不碰任何依赖）· `/health/db` 数据库 · `/health/redis` 缓存

> liveness 和 readiness 分开是有意的：合并的话数据库一抖动，编排系统会误判进程已死并疯狂重启，反而放大故障。

## 十一、项目结构

```
OpsPilot/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口、中间件、lifespan
│   │   ├── core/
│   │   │   ├── config.py           # 配置中心（.env 绝对路径加载）
│   │   │   ├── security.py         # bcrypt + JWT（含 jti）
│   │   │   ├── redis.py            # Redis 客户端 + JWT 黑名单（fail-open 降级）
│   │   │   ├── exceptions.py       # 统一异常体系 + 全局处理器
│   │   │   ├── deps.py             # DbSession / CurrentUser / require_roles
│   │   │   └── logging.py          # UTF-8 强制输出（Windows GBK 修复）
│   │   ├── db/
│   │   │   ├── session.py          # 异步引擎 + Session 工厂
│   │   │   └── base.py             # DeclarativeBase + 公共 Mixin
│   │   ├── models/                 # 11 张表：user / ticket / sla / agent
│   │   ├── schemas/                # Pydantic 出入参（白名单序列化）
│   │   ├── services/               # ★ 业务逻辑唯一归属地（7 个 Service）
│   │   ├── api/v1/                 # 路由层：health/auth/tickets/dashboard/agent
│   │   └── agent/                  # ★★ AI 层
│   │       ├── context.py          #   ToolContext（身份注入，LLM 不可伪造）
│   │       ├── policy.py           #   风险分级（纯代码，不问模型）
│   │       ├── state.py            #   AgentState + 意图/计划模型
│   │       ├── prompts.py          #   提示词集中管理
│   │       ├── llm.py              #   LLM 客户端（懒加载）
│   │       ├── nodes.py            #   5 个图节点
│   │       ├── graph.py            #   状态图组装 + 条件路由
│   │       ├── runner.py           #   图执行 → SSE 事件流（含心跳）
│   │       ├── executor.py         #   审批后的批量执行（SAVEPOINT）
│   │       ├── events.py           #   SSE 事件定义 + 帧格式化
│   │       └── tools/              #   read(6) / write(5) / batch(2) 工具
│   ├── alembic/                    # 迁移脚本
│   ├── scripts/                    # 种子数据 + 11 个验证脚本
│   ├── tests/
│   ├── Dockerfile                  # 分层缓存 + 非 root 用户 + 启动时迁移
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   │   ├── request.ts          # Axios 实例 + 双拦截器
│   │   │   ├── sse.ts              # ★ 手写 SSE 解析器（fetch + ReadableStream）
│   │   │   └── auth/ticket/dashboard/agent.ts
│   │   ├── stores/
│   │   │   ├── user.ts             # 登录态
│   │   │   └── agent.ts            # ★ SSE 事件流 → UI 状态映射
│   │   ├── types/                  # 与后端 Schema 对齐的 TS 类型
│   │   ├── router/index.ts         # 嵌套布局路由 + 导航守卫
│   │   ├── layouts/MainLayout.vue  # 侧边栏 + 顶栏 + AI 助手入口
│   │   ├── views/                  # 登录/注册/看板/工单列表/工单详情/404
│   │   └── components/
│   │       ├── ticket/TicketTags.vue
│   │       └── agent/
│   │           ├── AgentPanel.vue          # Copilot 对话面板
│   │           ├── AgentStepTimeline.vue   # 执行过程时间线
│   │           └── ActionConfirmCard.vue   # ★ HITL 确认卡片
│   ├── nginx.conf                  # ★ SSE 专用 location（proxy_buffering off）
│   ├── Dockerfile                  # 多阶段构建（1GB → 50MB）
│   └── package.json
├── docker-compose.yml              # 全栈部署（4 服务）
├── docker-compose.dev.yml          # 本地开发（MySQL:3307 + Redis:6380）
├── docker-compose.build.yml        # 国内镜像源构建覆盖
├── .env.example                    # 部署环境变量模板
├── PROJECT_ARCHITECTURE.md         # 架构设计文档（15 章节）
└── README.md                       # 本文件
```

## 十二、测试与验证

**259 项断言，全部通过。** 不是"我测过了"，是**你可以自己跑一遍**：

| 脚本 | 覆盖 | 断言数 | 需要 |
|---|---|---|---|
| `scripts/test_auth.py` | 注册查重/登录/密码错误/伪造 token/防枚举 | 31 | 后端 |
| `scripts/test_tickets.py` | 状态机/权限/SLA 重算/筛选/分页/越权 | 49 | 后端 + 种子数据 |
| `scripts/test_tools.py` | 13 个工具：schema 无身份泄漏/风险分级/越权 | 77 | 后端（**不需要 LLM**）|
| `scripts/test_logout.py` | JWT 黑名单：登出即失效/不误伤他人/幂等 | 13 | 后端 + Redis |
| `scripts/test_cache.py` | 统计缓存：写入/命中/★权限隔离/写失效/降级 | 14 | 后端 + Redis |
| `scripts/verify_ui.py` | 前端模块编译/代理链路/★SSE 真流式 | 46 | 前后端 |
| `scripts/test_agent.py` | ★4 个场景端到端 + 拒绝流程 + 幂等 | 29 | 后端 + **真实 LLM** |

辅助脚本：`test_llm.py`（LLM 连通性三连测）· `check_deps.py`（依赖完整性静态检查）· `smoke_test.py`（基础冒烟）

### 几个值得一提的验证点

```text
✓ 遍历全部工具，断言 schema 里不出现 user_id/config —— 身份隔离真的成立
✓ 断言 SSE 首事件到达时间远早于流结束 —— 证明真流式，没被代理缓冲
✓ 场景 4 验证：确认前数据未变、勾选 3 条只执行 3 条、重复提交被 409 拦截
✓ 登出后原 token 立即 401，另一个用户的 token 不受影响
✓ 普通用户改他人工单返回 403，改自己的放行
```

跑法：

```bash
cd backend
python scripts/test_tickets.py              # 单跑某一个
python scripts/test_agent.py --only batch   # 只跑场景 4（省 token）
```

## 十三、开发过程中解决的真实问题

这些问题全部是**真实跑起来才暴露**的，每一个都有对应的测试防回归。这也是这个项目的价值之一 —— 不是抄来的 Demo。

### 🔴 P0 · 模型在编造工单 ID

`list_tickets` 返回的字段里没有 `id`，但 `batch_update_tickets` 要的是 `ticket_ids`。测试发现计划里的 20 个 ID **是模型自己编的** —— 如果碰巧对应真实工单，就会改错数据且无从察觉。

**修复**：工具返回值补上 `id` 字段；提示词明确"ID 只能来自工具返回值"。
**教训**：一个工具的输出喂给另一个工具的输入时，**格式必须自洽**，不能靠模型脑补。

### 🔴 P0 · 静默漏数据

用户说"**所有**超过 24 小时的工单"（22 条），`list_tickets` 默认 `limit=20` —— 只处理了 20 条，漏掉 2 条，**没有任何报错**。

**修复**：limit 上限提到 100、返回 `truncated` 字段、提示词强制要求"批量操作前确认 truncated=false"。

### 🟠 P1 · 工单号占位撞唯一索引

`ticket_no` 先用空字符串占位、拿到自增 ID 后回填 —— 批量插入第二条就撞唯一索引；并发创建同样会炸。改成随机字符串占位。

### 🟠 P1 · Pydantic v2 校验器不触发

`TicketAssign` 用 `field_validator` 做"至少传一个字段"的校验 —— 但 v2 里字段没传时校验器根本不执行，空请求被放行。改用 `model_validator(mode="after")`。

### 🟠 P1 · exclude_unset 挡不住显式 None

Agent 工具层显式构造 `TicketUpdate(category=None, ...)`，`exclude_unset=True` 认为"传了"，`category=null` 被写库撞 NOT NULL 约束。**REST 接口永远测不出来**（前端只会省略字段），只有第二个入口调用同一 Service 时才暴露。修复：`exclude_unset=True, exclude_none=True`。

### 🟠 P1 · 解析被截断的 JSON

为控制 token 把工具结果截断到 3000 字符，之后又想解析这段 JSON 生成确认卡片预览 —— JSON 已被腰斩，必然解析失败且**失败得很安静**。修复：工具执行时就存结构化数据进 `observations`，不事后解析展示文本。

### 🟡 P2 · "在我机器上能跑"

`email-validator` 是早先手动 `pip install` 的，从没进 `requirements.txt`。本地一切正常，Docker 全新安装直接启动崩溃。**修复后写了 `check_deps.py`**：静态扫描代码 import 的第三方包，对照依赖清单报遗漏。

### 🟡 P2 · 环境类疑难（不报错，只是行为诡异）

| 现象 | 根因 |
|---|---|
| httpx 报 502，curl 却正常 | httpx 读 **Windows 注册表**里的系统代理，把 127.0.0.1 的请求发给了代理 |
| 脚本读不到 LLM 配置 | `.env` 用相对路径，换目录执行就静默用默认值（现在改为绝对路径加载）|
| Vite 页面打不开 | 默认 host 解析成 IPv6，`127.0.0.1` 连不上 |
| alembic 启动即崩 | `alembic.ini` 里的中文注释被 GBK 解析失败（该文件必须纯 ASCII）|
| Redis 连接被拒 | protected mode 默认只接受容器内回环连接 |
| Docker 构建 SSL 失败 | Docker Desktop 把系统代理注入构建环境，代理链路不通 |

## 十四、技术决策记录

| # | 决策 | 理由 |
|---|---|---|
| 1 | Agent 工具必须走 Service，禁止碰 ORM | AI 入口与人工入口共享权限/状态机/审计，且互相暴露盲区（P1 bug 就是证明）|
| 2 | 身份通过 `RunnableConfig` 注入，不做工具参数 | 从"模型可填的参数"变成"代码决定的事实" |
| 3 | 风险分级用纯代码，不调 LLM | 安全边界不能是概率性的；未知工具默认最严格（fail-safe）|
| 4 | HITL 确认「计划」而非「结果」 | 用户要看到具体工单才能核对，数字没法核对 |
| 5 | 两阶段 HITL，不用 interrupt+Checkpointer | 等待确认是业务状态，存 MySQL 可查询/审计/超时清理 |
| 6 | 批量执行用 SAVEPOINT 而非整体回滚 | 17 条里 1 条失败，不该让另外 16 条白改 |
| 7 | 手写 SSE 解析，不用 EventSource | EventSource 带不了 JWT、只能 GET、不能取消 |
| 8 | 前端也有一份状态机副本 | 后端为了安全（权威），前端为了体验（只显示合法选项）|
| 9 | JWT 黑名单 fail-open | Redis 挂了不应导致全站无法登录；登出功能短暂降级可接受 |
| 10 | 意图 → 工具白名单 | 选项越少模型越不会选错；比提示词千叮万嘱更可靠 |
| 11 | `create_ticket` 不需要确认 | 新增可撤销；什么都弹确认会产生确认疲劳，削弱真正重要的确认 |
| 12 | 健康检查 liveness/readiness 分离 | 防止依赖抖动引发编排系统疯狂重启 |
| 13 | 缓存 key 带权限维度（`all` / `u{id}`） | 数据权限因人而异 —— key 不带用户维度，普通用户会先写入"只含自己工单"的统计，admin 命中后看到错误数据（缓存投毒越权）|
| 14 | 统计缓存 = 写失效 + TTL 兜底 | 主动删除存在竞态（旧请求把旧数据写回），TTL 封底；版本号 key 方案作为改进方向 |
| 15 | 缓存与黑名单同一 fail-open 策略 | 缓存是加速器不能变单点，Redis 故障回源查库 |

## 十五、环境变量

### 后端 `backend/.env`（本地开发）

```ini
# 数据库（对应 docker-compose.dev.yml 暴露的宿主机端口）
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3307
MYSQL_USER=opspilot
MYSQL_PASSWORD=opspilot123
MYSQL_DATABASE=opspilot

# Redis（JWT 黑名单）
REDIS_URL=redis://127.0.0.1:6380/0

# 大模型（OpenAI 兼容）
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=qwen-plus
LLM_TEMPERATURE=0.1        # Agent 场景低温，稳定调工具

# Agent 行为（安全阀）
AGENT_MAX_STEPS=12          # 最大工具轮数
AGENT_MAX_TOKENS_PER_RUN=50000
AGENT_RUN_TIMEOUT=120       # 秒
AGENT_BATCH_LIMIT=100       # 批量操作单次上限
AGENT_APPROVAL_TIMEOUT=600  # 确认有效期（秒）
```

### 部署 `.env`（项目根目录，docker compose 用）

见 [.env.example](./.env.example) —— 数据库密码、SECRET_KEY、LLM 配置。

> 完整的可切换服务商清单（DeepSeek/智谱/Kimi/Ollama）见 `backend/.env.example` 内注释。

## 十六、常见问题

<details>
<summary><b>后端起不来，报 ModuleNotFoundError: app</b></summary>

必须在 `backend/` 目录下启动 uvicorn。`app` 包在 backend 里，Python 从当前目录找模块。
</details>

<details>
<summary><b>改了代码但行为没变</b></summary>

大概率是旧的 uvicorn 进程还占着 8000 端口。检查并清理：

```powershell
netstat -ano | Select-String ":8000.*LISTENING"
# 拿到 PID 后
taskkill /F /T /PID <PID>
```
</details>

<details>
<summary><b>Agent 接口返回 503「还没有配置大模型 API Key」</b></summary>

`backend/.env` 里 `LLM_API_KEY` 没填或还是占位符。填好后**重启后端**。可以先跑 `python scripts/test_llm.py` 验证 Key 有效性（三连测：基础对话/结构化输出/工具调用）。
</details>

<details>
<summary><b>PowerShell 里中文日志乱码</b></summary>

代码已强制 UTF-8 输出，旧版终端可能仍乱码。执行 `chcp 65001` 或改用 Windows Terminal。
</details>

<details>
<summary><b>测试脚本报 502 / 连接被拒</b></summary>

如果你开着 Clash 等代理：httpx 会读 Windows 系统代理。本项目所有测试脚本已加 `trust_env=False`，自己写脚本时记得加。
</details>

<details>
<summary><b>Docker 构建报 PACKAGES DO NOT MATCH THE HASHES</b></summary>

网络抖动导致包下载损坏，pip 哈希校验拦住了（这是保护）。重跑 `docker compose build backend` 即可，通常一两次就过。
</details>

<details>
<summary><b>页面能开但 AI 助手一直转圈</b></summary>

按 F12 → Network 看请求：若 `chat` 请求长时间无响应最后一次性出现 —— 代理缓冲了 SSE。本地开发检查 Vite 代理；Docker 部署检查 `nginx.conf` 的 SSE location 是否有 `proxy_buffering off`。
</details>

## 十七、已知不足与后续规划

诚实列出，也是面试时可以主动谈的改进方向：

| 不足 | 现状 | 改进方向 |
|---|---|---|
| 无法踢掉某用户全部会话 | 黑名单按单 token 粒度 | 用户级"签发时间戳"，校验 token iat 是否早于它 |
| 批量执行同步阻塞 | 100 条内可接受 | 分批 + 异步任务队列（但注意不过度工程化）|
| 无附件上传 | — | MinIO + 类型/大小校验 |
| 定时任务无分布式锁 | 单副本下正确 | Redis SETNX 锁或独立调度服务 |
| 无消息通知 | — | 邮件/Webhook（`ticket_events` 已是现成的事件源）|
| 前端未做 Agent 执行过程的历史回放 UI | 后端 `GET /agent/runs/{id}` 已支持 | 时间线组件复用 `agent_run_steps` 数据 |
| 工单无标签/自定义字段 | — | JSON 字段 + 动态表单 |

**刻意不做的**（防止过度工程化，详见架构文档）：MCP、Multi-Agent、DeepAgents、RAG、向量数据库、微服务。工单查询是结构化精确查询，SQL 是正确工具；四个场景单 Agent 单图足够。

---

## 相关文档

- 📐 [PROJECT_ARCHITECTURE.md](./PROJECT_ARCHITECTURE.md) —— 完整架构设计（15 章节：产品定位/数据模型/Agent 架构/SSE 事件/HITL 流程/风险清单）
- 🔧 [backend/README.md](./backend/README.md) —— 后端开发指南（Windows 踩坑记录）

---

**Demo 演示建议**：按「场景 1 → 场景 2 → 场景 4」顺序，场景 4 的确认卡片是整个项目技术含量最高的部分。
