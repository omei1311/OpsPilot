# OpsPilot 架构设计文档

> 企业智能工单与运营协同平台
> 版本：v1.0（架构冻结版）
> 目标岗位：大模型应用开发 / Agent 应用开发实习生

---

## 目录

1. [产品定位](#1-产品定位)
2. [用户角色](#2-用户角色)
3. [核心业务流程](#3-核心业务流程)
4. [系统架构](#4-系统架构)
5. [前后端目录结构](#5-前后端目录结构)
6. [数据库实体关系](#6-数据库实体关系)
7. [API 模块划分](#7-api-模块划分)
8. [Agent 架构](#8-agent-架构)
9. [Agent State 设计](#9-agent-state-设计)
10. [Tool 划分](#10-tool-划分)
11. [SSE 事件设计](#11-sse-事件设计)
12. [Human-in-the-loop 流程](#12-human-in-the-loop-流程)
13. [开发阶段](#13-开发阶段)
14. [技术风险](#14-技术风险)
15. [暂时不做的功能](#15-暂时不做的功能)

---

## 1. 产品定位

### 1.1 一句话定位

**OpsPilot 是一套企业工单业务系统，内置一个能"看懂业务、动手干活、但动手前先请示"的 AI Copilot。**

### 1.2 为什么不是 Todo List，也不是聊天机器人

| 维度 | Todo List | 聊天机器人 | OpsPilot |
|---|---|---|---|
| 数据模型 | 单表 | 无 | 9 张业务表 + 3 张 Agent 表，有状态机与 SLA |
| 核心能力 | CRUD | 文本生成 | CRUD + 状态机 + SLA 引擎 + 统计聚合 |
| AI 角色 | 无 | 主体 | **副驾驶**：业务系统是主体，AI 是操作入口之一 |
| AI 权限 | — | 只能说话 | **能读能写**，但写操作受权限、范围、审批三层约束 |
| 失败影响 | 无 | 答错话 | 改错数据，因此必须有 HITL、审计日志、幂等 |
| 技术深度 | 低 | 中（调 API） | 高：Tool Calling + State 机 + 结构化输出 + 流式 + 中断恢复 |

### 1.3 核心价值主张

传统工单系统的痛点是**"人适应系统"**：必须点开表单、逐字段填写、逐条筛选、逐条改状态。
OpsPilot 让**"系统适应人"**：用户用自然语言表达意图，Agent 负责翻译成结构化业务操作。

关键差异点在于 —— **Agent 不是绕过业务系统的旁路，而是业务系统的另一个客户端**：

```
人工操作路径：  Vue 页面 → REST API → Service 层 → 数据库 + 审计日志
Agent 操作路径：自然语言 → LangGraph → Tool → Service 层 → 数据库 + 审计日志
                                            ↑
                                    同一套 Service、同一套权限、同一套审计
```

这一点是整个架构的**设计红线**：Tool 层不允许直接操作 ORM 会话，必须复用 Service 层。这保证了"人改的"和"AI 改的"在数据一致性、权限校验、日志留痕上完全等价。

### 1.4 四个核心 Agent 场景（验收基准）

| # | 用户输入 | 意图 | 涉及能力 | 是否需确认 |
|---|---|---|---|---|
| 1 | "线上支付接口大量出现 502，帮我报个故障。" | 创建工单 | 实体抽取 → 槽位补全 → `create_ticket` | 否（低风险，创建后回执） |
| 2 | "帮我看看最近有哪些高优先级工单没处理。" | 查询 | 意图识别 → `query_tickets` → 摘要渲染 | 否（只读） |
| 3 | "分析最近 7 天的 SLA 风险。" | 分析 | `get_sla_risk_report` → 聚合 → 结论 + 图表数据 | 否（只读） |
| 4 | "把所有超过 24 小时未处理的工单升级成高优先级，并分配给技术部门。" | 批量修改 | 查询候选集 → **生成计划** → **等待人工确认** → 批量执行 | **是（强制）** |

场景 4 是整个项目的技术制高点，也是面试时最值得讲的点。

### 1.5 项目规模约束（自我约束，防止过度工程）

- 后端**单体应用**，不拆微服务，所有模块在一个 FastAPI 进程中
- 数据库**单库单 schema**，不用分库分表
- Agent **单 Agent 单图**，不用 Multi-Agent
- 依赖**不超过 30 个直接依赖**，锁死版本
- 本地 `docker compose up` 一条命令起全栈，冷启动 ≤ 60 秒
- 代码总量目标 6000~9000 行（含前端），**能在两周内讲清楚每一行**

---

## 2. 用户角色

### 2.1 角色定义

采用**扁平 RBAC**：用户直接绑定一个角色，角色权限硬编码在代码里（`app/core/permissions.py`），不做动态权限表——这是刻意的简化。

| 角色 | code | 职责 | 数据范围 |
|---|---|---|---|
| 系统管理员 | `admin` | 用户/部门/分类/SLA 策略管理，可查看全部工单 | 全部 |
| 服务台受理员 | `operator` | 接待、创建、分派、转派、关闭工单 | 本部门 + 自己创建/被分派的 |
| 处理工程师 | `engineer` | 处理被分派的工单、填写处理记录 | 仅自己名下的工单 |
| 部门主管 | `manager` | 查看本部门统计、审批升级、批量操作 | 本部门全部 |

> `admin` 与 `manager` 是唯一允许执行**批量写操作**的 Role 组合，其余角色即使通过 Agent 请求批量修改，也会在 **Policy 层**被拒绝（而非依赖前端隐藏按钮）。

### 2.2 部门模型

部门是**扁平两层结构**（`departments.parent_id` 可空），实际使用中只用到一层：技术部、客服部、运维部、产品部。保留 `parent_id` 字段是为了数据模型完整，**不实现递归查询**。

### 2.3 权限校验层次

```
第 1 层  JWT 认证          → 你是谁（api/deps.py: get_current_user）
第 2 层  角色路由守卫       → 这个接口角色能不能进（require_roles）
第 3 层  数据范围过滤       → 你能看到哪些行（service 层 scope 参数）
第 4 层  业务规则校验       → 这条数据你能不能改（service 层状态机 + 归属校验）
第 5 层  Agent Policy     → AI 代你操作时，是否降级/需确认/直接拒绝（agent/policy.py）
```

**第 5 层是 Agent 特有的，也是最容易被忽略的**。举例：`engineer` 让 Agent "把所有工单关闭"，Agent 不能因为"用户点了发送"就执行——Policy 层必须在 Tool 执行前拦截。

---

## 3. 核心业务流程

### 3.1 工单生命周期状态机

```mermaid
stateDiagram-v2
    [*] --> pending_assign: 创建 / Agent 创建
    pending_assign --> processing: 分派给处理人
    processing --> pending_assign: 转派 / 驳回
    processing --> pending: 挂起（等待用户反馈）
    pending --> processing: 恢复
    processing --> resolved: 提交解决方案
    resolved --> closed: 验收通过 / 自动关闭
    resolved --> processing: 验收驳回
    pending_assign --> cancelled: 取消（仅创建人/管理员）
    processing --> cancelled: 取消（仅管理员）
    closed --> [*]
    cancelled --> [*]
```

**状态枚举**（存库值）：`pending_assign` / `processing` / `pending` / `resolved` / `closed` / `cancelled`

**状态流转表**（硬编码在 `services/ticket_state.py`，越权流转抛 `InvalidTransitionError`）：

| 当前状态 | 允许流转到 | 允许角色 |
|---|---|---|
| `pending_assign` | `processing`, `cancelled` | admin, manager, operator |
| `processing` | `pending`, `resolved`, `pending_assign` | admin, manager, 工单处理人 |
| `pending` | `processing`, `cancelled` | admin, manager, 工单处理人 |
| `resolved` | `closed`, `processing` | admin, manager, 创建人 |
| `closed` | — | 终态 |
| `cancelled` | — | 终态 |

**设计原则：状态机是唯一入口**。无论是 REST API 还是 Agent Tool，改状态都必须调用 `TicketService.transition(ticket_id, to_status, actor, reason)`，禁止直接赋值 `ticket.status = ...`。

### 3.2 主流程：人工提单 → 分派 → 处理 → 关闭

```mermaid
sequenceDiagram
    participant U as 受理员
    participant FE as Vue 前端
    participant API as FastAPI
    participant SVC as TicketService
    participant DB as MySQL
    participant SCH as SLA Scheduler

    U->>FE: 填写工单表单
    FE->>API: POST /api/v1/tickets
    API->>SVC: create_ticket(payload, reporter)
    SVC->>SVC: 匹配分类 → 加载 SLA 策略 → 计算 due_at
    SVC->>DB: INSERT tickets + ticket_logs
    SVC-->>API: TicketVO
    API-->>FE: 201 Created
    SCH->>DB: 每 5 分钟扫描，刷新 sla_status / is_overdue
    U->>FE: 分派给工程师
    FE->>API: POST /tickets/{id}/assign
    API->>SVC: transition + assign
    SVC->>DB: UPDATE + INSERT ticket_logs
```

### 3.3 主流程：Agent 创建工单（场景 1）

```mermaid
sequenceDiagram
    participant U as 用户
    participant API as FastAPI /agent/chat
    participant G as LangGraph
    participant LLM as LLM
    participant T as Tool: create_ticket
    participant SVC as TicketService

    U->>API: "线上支付接口大量出现 502，帮我报个故障。"
    API->>G: astream(初始 State)
    G->>LLM: 意图识别 + 实体抽取（structured output）
    LLM-->>G: intent=create_ticket, entities={title, category_hint, priority_hint}
    G->>G: 槽位补全检查（缺 assignee_department → 用分类默认值兜底）
    G->>T: create_ticket(title, description, category, priority)
    T->>SVC: 复用人工创建路径，reporter=当前用户，source=agent
    SVC-->>T: ticket_no = OPS-2026-000123
    T-->>G: {ok, ticket_no, url}
    G->>LLM: 生成回执话术
    G-->>API: answer
    API-->>U: SSE: tool_call → tool_result → token... → done
```

**关键点**：`create_ticket` 是**低风险写操作，不触发 HITL**——因为创建一条新数据不破坏已有数据，且可撤销（取消工单）。这是风险分级的设计依据，详见 [12. Human-in-the-loop 流程](#12-human-in-the-loop-流程)。

### 3.4 主流程：Agent 批量修改（场景 4，核心）

```
用户: "把所有超过 24 小时未处理的工单升级成高优先级，并分配给技术部门。"
   │
   ├─ [只读阶段] query_tickets(status=pending_assign, older_than=24h)
   │     └─ 返回 17 条候选工单（只读，无需确认）
   │
   ├─ [规划阶段] LLM 生成结构化 Plan
   │     └─ Plan = [
   │          {tool: batch_update_tickets, args:{ids:[...], priority:high}, risk:high},
   │          {tool: batch_assign_tickets, args:{ids:[...], dept:技术部}, risk:high}
   │        ]
   │
   ├─ [中断] 写入 agent_pending_actions(status=pending) → SSE 推送 plan 事件
   │     └─ 前端渲染「变更确认卡片」：17 条工单，2 步操作，逐条可勾选
   │
   ├─ ⏸ 图挂起（LangGraph interrupt），HTTP 连接关闭，状态持久化到 Redis
   │
   └─ [恢复] 用户点「确认执行」
         └─ POST /agent/actions/{id}/approve → 恢复图 → 事务内批量执行
               └─ 每执行一条，SSE 推送 action_executed 事件
```

**这里体现了三个工程决策**：
1. **读操作直接执行，写操作才确认** —— 避免"查个东西也要点确认"的糟糕体验
2. **确认的是"计划"而不是"结果"** —— 用户在数据被改之前看到完整工单列表
3. **执行过程逐条流式反馈** —— 17 条工单不会让用户盯着转圈等 10 秒

### 3.5 SLA 计算流程

```
SLA 策略（sla_policies）：
  priority=urgent, first_response_minutes=15,  resolve_minutes=240
  priority=high,   first_response_minutes=60,  resolve_minutes=480
  priority=medium, first_response_minutes=240, resolve_minutes=1440
  priority=low,    first_response_minutes=480, resolve_minutes=2880

创建工单时：
  response_due_at = created_at + policy.first_response_minutes
  resolve_due_at  = created_at + policy.resolve_minutes
  除 created_at 外，不计算工作日历（简化为自然小时）

SLA 状态（sla_status，由定时任务刷新）：
  on_track  ：未超期且剩余 > 20% 时限
  at_risk   ：剩余 ≤ 20% 时限
  breached  ：已超期
  met       ：在 due_at 前完成（首响 / 解决）
```

**定时任务**：使用 **APScheduler**（进程内，`AsyncIOScheduler`）每 5 分钟扫描一次。不引入 Celery——单实例部署下 Celery 是纯负担。风险与降级方案见 [14. 技术风险](#14-技术风险)。

---

## 4. 系统架构

### 4.1 整体架构图

```mermaid
flowchart TB
    subgraph Client["浏览器"]
        VUE["Vue3 SPA<br/>Element Plus + ECharts + Pinia"]
    end

    subgraph Docker["Docker Compose 网络"]
        NGINX["nginx:alpine<br/>静态资源 + API 反向代理<br/>SSE 专用 location"]
        subgraph BE["backend 容器 (FastAPI)"]
            API["API 层<br/>api/v1/*"]
            SVC["Service 层<br/>业务逻辑 / 状态机 / 权限"]
            AGENT["Agent 层<br/>LangGraph 单图"]
            TOOLS["Tool 层<br/>读工具 / 写工具"]
            SCHED["APScheduler<br/>SLA 扫描"]
        end
        MYSQL[("MySQL 8.0<br/>业务数据 + Agent 运行记录")]
        REDIS[("Redis 7<br/>会话缓存 + 图 Checkpoint + 事件序号")]
    end

    LLM["LLM API<br/>OpenAI 兼容<br/>（外部）"]

    VUE -->|HTTPS / SSE| NGINX
    NGINX --> API
    API --> SVC
    API --> AGENT
    AGENT --> TOOLS
    TOOLS --> SVC
    SVC --> MYSQL
    SVC --> REDIS
    AGENT --> REDIS
    AGENT -->|HTTP| LLM
    SCHED --> SVC
```

### 4.2 分层职责（严格单向依赖）

| 层 | 目录 | 职责 | 禁止 |
|---|---|---|---|
| API | `app/api/v1/` | 参数校验、鉴权依赖、调用 Service、组装响应 | 禁止写业务逻辑、禁止直接查库 |
| Service | `app/services/` | 业务规则、状态机、事务边界、权限范围过滤 | 禁止感知 HTTP（不 import Request） |
| Model | `app/models/` | SQLAlchemy ORM 映射 | 禁止写业务方法 |
| Schema | `app/schemas/` | Pydantic 出入参 | 禁止引用 ORM 对象做序列化（用 `from_attributes`） |
| Agent | `app/agent/` | 图编排、状态、工具、提示词、策略 | **禁止直接访问 ORM Session**，只能调 Service |
| Core | `app/core/` | 配置、安全、依赖、异常、日志 | 无 |

**依赖方向**：`api → services → models`，`agent → tools → services → models`。
Agent 层与 API 层**互不依赖**，是两个平行的入口，共享同一个 Service 层。这是本架构最重要的一张图：

```
        ┌──────────────┐        ┌──────────────┐
        │  REST API    │        │  Agent Graph │
        │  (人工入口)   │        │  (AI 入口)    │
        └──────┬───────┘        └──────┬───────┘
               │                       │
               └───────────┬───────────┘
                           ▼
                   ┌───────────────┐
                   │  Service 层    │  ← 权限 / 状态机 / 事务 / 审计
                   └───────┬───────┘
                           ▼
                   ┌───────────────┐
                   │  MySQL/Redis  │
                   └───────────────┘
```

### 4.3 技术选型理由

| 技术 | 选它的理由 | 不选什么 |
|---|---|---|
| FastAPI | 原生 async、Pydantic 校验、`StreamingResponse` 做 SSE 天然合适、自动 OpenAPI 文档 | 不用 Django（重）、Flask（缺 async/校验） |
| SQLAlchemy 2.x + Alembic | 2.0 的 `select()` 风格类型友好、Alembic 管迁移 | 不用 Tortoise（生态小） |
| MySQL | 岗位 JD 常见、JSON 字段够用、运维熟悉 | 不用 PostgreSQL（本地环境/岗位匹配度） |
| Redis | 一举三得：会话缓存、LangGraph checkpoint、SSE 事件缓冲 | 不额外引 MQ |
| LangGraph | 显式状态图 + `interrupt()` 原生支持 HITL，比裸写 while 循环可维护得多 | 不用 AutoGen、不用 CrewAI（多 Agent 场景才需要） |
| Element Plus | 表格/表单/抽屉组件齐全，管理后台开发速度最快 | 不用 Naive/AntdV（生态相比稍弱） |
| Pinia + Axios | Vue3 官方标配 | 不用 Vuex |

### 4.4 Docker Compose 拓扑

```yaml
services:
  mysql:      # mysql:8.0,  volume 持久化, healthcheck: mysqladmin ping
  redis:      # redis:7-alpine, appendonly yes
  backend:    # python:3.11-slim, depends_on: mysql/redis (healthy)
              # command: alembic upgrade head && uvicorn app.main:app
  frontend:   # 多阶段构建: node:20 build → nginx:alpine 托管 dist
              # nginx 同时反代 /api → backend:8000
```

**冷启动顺序**：`mysql(healthy) → redis(healthy) → backend(migrate+serve) → frontend`

**SSE 关键配置**（nginx，写错就是"前端一直转圈不输出"的经典坑）：

```nginx
location /api/v1/agent/stream {
    proxy_pass http://backend:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    proxy_buffering off;          # 关键：关闭缓冲
    proxy_cache off;
    proxy_read_timeout 300s;      # 长连接，别用默认 60s
    chunked_transfer_encoding off;
}
```

后端同时返回 `X-Accel-Buffering: no` 响应头做双保险。

---

## 5. 前后端目录结构

### 5.1 后端目录结构

```
backend/
├── app/
│   ├── main.py                       # FastAPI 实例、中间件、路由注册、lifespan(启动 Scheduler)
│   ├── core/
│   │   ├── config.py                 # pydantic-settings，读 .env
│   │   ├── security.py               # 密码哈希(bcrypt) / JWT 签发校验
│   │   ├── deps.py                   # get_db / get_current_user / require_roles
│   │   ├── permissions.py            # 角色-权限矩阵、数据范围 scope 计算
│   │   ├── exceptions.py             # BizError 体系 + 全局异常处理器
│   │   └── logging.py                # 结构化日志 + trace_id 注入
│   │
│   ├── db/
│   │   ├── session.py                # async engine / sessionmaker
│   │   └── base.py                   # DeclarativeBase + 公共 Mixin(id, created_at, updated_at)
│   │
│   ├── models/                       # SQLAlchemy ORM
│   │   ├── user.py                   # User, Department
│   │   ├── ticket.py                 # Ticket, TicketCategory, TicketComment, TicketLog
│   │   ├── sla.py                    # SlaPolicy
│   │   └── agent.py                  # AgentConversation, AgentMessage, AgentRun, AgentRunStep,
│   │                                 #   AgentPendingAction
│   │
│   ├── schemas/                      # Pydantic 出入参
│   │   ├── common.py                 # PageResult, ApiResponse, 枚举
│   │   ├── auth.py / user.py / ticket.py / dashboard.py / agent.py
│   │
│   ├── services/                     # ★ 业务逻辑唯一归属地
│   │   ├── auth_service.py
│   │   ├── user_service.py
│   │   ├── ticket_service.py         # 创建/查询/更新/分派/转派
│   │   ├── ticket_state.py           # 状态机定义与流转校验
│   │   ├── sla_service.py            # due_at 计算、sla_status 刷新
│   │   ├── stats_service.py          # Dashboard 聚合查询
│   │   └── audit_service.py          # ticket_logs / run 记录写入
│   │
│   ├── api/
│   │   ├── v1/
│   │   │   ├── __init__.py           # api_router 汇总
│   │   │   ├── auth.py               # /auth/*
│   │   │   ├── users.py              # /users/*  /departments/*
│   │   │   ├── tickets.py            # /tickets/*
│   │   │   ├── categories.py         # /categories/*
│   │   │   ├── sla.py                # /sla/policies/*
│   │   │   ├── dashboard.py          # /dashboard/*
│   │   │   └── agent.py              # /agent/*  ← 含 SSE 端点
│   │   └── deps.py
│   │
│   ├── agent/                        # ★ AI 层
│   │   ├── graph.py                  # StateGraph 构建、编译、checkpointer 装配
│   │   ├── state.py                  # AgentState / Plan / PlanStep 定义
│   │   ├── nodes/
│   │   │   ├── understand.py         # 意图识别 + 实体抽取（structured output）
│   │   │   ├── plan.py               # 生成执行计划（只读工具可直接调）
│   │   │   ├── act.py                # 工具执行节点（ToolNode 包装 + 审计）
│   │   │   ├── confirm.py            # HITL 中断节点（写 pending_action + interrupt）
│   │   │   ├── execute.py            # 审批通过后的批量执行节点
│   │   │   └── respond.py            # 最终回答生成 + token 流式
│   │   ├── tools/
│   │   │   ├── registry.py           # 工具注册表 + 风险分级元数据
│   │   │   ├── read_tools.py         # 无副作用工具
│   │   │   ├── write_tools.py        # 有副作用工具
│   │   │   └── schemas.py            # 每个工具的 Pydantic 入参/出参
│   │   ├── policy.py                 # 权限裁剪、风险判定、是否需要人工确认
│   │   ├── prompts.py                # 系统提示词、few-shot
│   │   ├── llm.py                    # ChatOpenAI 客户端工厂（超时/重试/降级）
│   │   ├── events.py                 # SSE 事件模型 + emitter
│   │   └── runner.py                 # 图执行入口：astream → 翻译成 SSE 事件
│   │
│   └── scheduler/
│       └── jobs.py                   # SLA 扫描任务
│
├── alembic/
│   ├── env.py
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── test_auth.py / test_tickets.py / test_sla.py
│   ├── test_agent_tools.py           # 工具层单测（mock Service）
│   └── test_agent_graph.py           # 图执行 + HITL 恢复
├── scripts/
│   └── seed.py                       # 演示数据：4 部门 / 8 用户 / 60 工单 / 6 分类
├── .env.example
├── Dockerfile
├── requirements.txt
└── pyproject.toml
```

### 5.2 前端目录结构

```
frontend/
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── api/
│   │   ├── request.ts                # Axios 实例：token 注入、401 跳转、错误提示
│   │   ├── auth.ts / ticket.ts / dashboard.ts / user.ts / agent.ts
│   │   └── sse.ts                    # ★ fetch + ReadableStream 的 SSE 客户端
│   ├── router/
│   │   ├── index.ts
│   │   └── guards.ts                 # 登录守卫 + 角色守卫
│   ├── stores/
│   │   ├── user.ts                   # token / profile / 权限判断
│   │   ├── ticket.ts                 # 列表筛选条件、分页
│   │   └── agent.ts                  # ★ 会话、消息流、待确认动作
│   ├── views/
│   │   ├── login/LoginView.vue
│   │   ├── dashboard/DashboardView.vue
│   │   ├── ticket/
│   │   │   ├── TicketListView.vue
│   │   │   ├── TicketDetailView.vue
│   │   │   └── TicketCreateDialog.vue
│   │   ├── agent/CopilotView.vue      # ★ 全屏 Copilot
│   │   └── admin/
│   │       ├── UserManageView.vue
│   │       ├── CategoryView.vue
│   │       └── SlaPolicyView.vue
│   ├── components/
│   │   ├── layout/AppLayout.vue / SideMenu.vue / HeaderBar.vue
│   │   ├── ticket/TicketTable.vue / TicketStatusTag.vue / TicketPriorityTag.vue
│   │   ├── chart/TrendChart.vue / SlaRiskChart.vue / PriorityPie.vue
│   │   └── agent/
│   │       ├── AgentChatPanel.vue      # 聊天容器（可嵌入右下角抽屉）
│   │       ├── MessageBubble.vue
│   │       ├── AgentStepTimeline.vue   # ★ 执行过程可视化（thought/tool/result）
│   │       ├── ToolCallCard.vue        # 工具调用卡片（可折叠看参数与结果）
│   │       └── ActionConfirmCard.vue   # ★ HITL 确认卡片
│   ├── composables/
│   │   ├── useAgentStream.ts           # SSE 事件 → store 的映射
│   │   └── useConfirm.ts
│   ├── types/                          # 与后端 schema 对齐的 TS 类型
│   │   ├── api.d.ts / ticket.d.ts / agent.d.ts
│   ├── utils/format.ts / constants.ts
│   └── styles/index.scss
├── index.html
├── vite.config.ts                      # dev proxy: /api → localhost:8000
├── tsconfig.json
├── Dockerfile
├── nginx.conf
└── package.json
```

---

## 6. 数据库实体关系

### 6.1 ER 图

```mermaid
erDiagram
    DEPARTMENT ||--o{ USER : "belongs to"
    USER ||--o{ TICKET : "reports"
    USER ||--o{ TICKET : "assigned to"
    DEPARTMENT ||--o{ TICKET : "handled by"
    TICKET_CATEGORY ||--o{ TICKET : "classifies"
    SLA_POLICY ||--o{ TICKET : "governs"
    TICKET ||--o{ TICKET_COMMENT : "has"
    TICKET ||--o{ TICKET_LOG : "has"
    USER ||--o{ TICKET_COMMENT : "writes"
    USER ||--o{ AGENT_CONVERSATION : "owns"
    AGENT_CONVERSATION ||--o{ AGENT_MESSAGE : "contains"
    AGENT_CONVERSATION ||--o{ AGENT_RUN : "contains"
    AGENT_RUN ||--o{ AGENT_RUN_STEP : "contains"
    AGENT_RUN ||--o| AGENT_PENDING_ACTION : "may require"
    USER ||--o{ AGENT_PENDING_ACTION : "approves"

    DEPARTMENT {
        bigint id PK
        varchar name
        varchar code UK
        bigint parent_id FK
    }
    USER {
        bigint id PK
        varchar username UK
        varchar password_hash
        varchar real_name
        varchar email
        enum role "admin/manager/operator/engineer"
        bigint department_id FK
        tinyint is_active
    }
    TICKET_CATEGORY {
        bigint id PK
        varchar name
        varchar code UK
        enum default_priority
        bigint default_sla_policy_id FK
        bigint default_department_id FK
    }
    SLA_POLICY {
        bigint id PK
        varchar name
        enum priority
        int first_response_minutes
        int resolve_minutes
        tinyint is_active
    }
    TICKET {
        bigint id PK
        varchar ticket_no UK "OPS-2026-000123"
        varchar title
        text description
        bigint category_id FK
        enum priority "low/medium/high/urgent"
        enum status "pending_assign/processing/pending/resolved/closed/cancelled"
        enum source "web/agent/api"
        bigint reporter_id FK
        bigint assignee_id FK
        bigint department_id FK
        bigint sla_policy_id FK
        datetime response_due_at
        datetime resolve_due_at
        datetime first_responded_at
        datetime resolved_at
        datetime closed_at
        enum sla_status "on_track/at_risk/breached/met/na"
        tinyint is_overdue
        varchar tags "JSON"
    }
    TICKET_COMMENT {
        bigint id PK
        bigint ticket_id FK
        bigint author_id FK
        enum author_type "user/agent/system"
        enum comment_type "reply/internal_note/agent_note"
        text content
    }
    TICKET_LOG {
        bigint id PK
        bigint ticket_id FK
        bigint actor_id FK
        enum actor_type "user/agent/system"
        varchar action "create/assign/transition/update/comment/batch_update"
        varchar field
        varchar old_value
        varchar new_value
        varchar reason
        varchar trace_id "Agent 运行关联"
    }
    AGENT_CONVERSATION {
        bigint id PK
        bigint user_id FK
        varchar title
        datetime last_active_at
    }
    AGENT_MESSAGE {
        bigint id PK
        bigint conversation_id FK
        enum role "user/assistant/tool/system"
        text content
        varchar tool_calls "JSON"
        bigint run_id FK
    }
    AGENT_RUN {
        bigint id PK
        varchar run_id UK "uuid"
        bigint conversation_id FK
        bigint user_id FK
        text user_input
        varchar intent
        varchar status "running/awaiting_approval/success/rejected/failed/cancelled"
        text plan_json
        text result_summary
        varchar error
        int prompt_tokens
        int completion_tokens
        datetime started_at
        datetime finished_at
    }
    AGENT_RUN_STEP {
        bigint id PK
        bigint run_id FK
        int seq
        enum step_type "intent/thought/tool_call/tool_result/plan/approval/answer/error"
        varchar node
        varchar tool_name
        varchar input_json
        varchar output_json
        varchar status
        int duration_ms
    }
    AGENT_PENDING_ACTION {
        bigint id PK
        varchar action_id UK
        bigint run_id FK
        bigint conversation_id FK
        bigint requested_by FK
        varchar title
        enum risk_level "low/medium/high"
        text payload_json "确认与执行所需的完整计划"
        int affected_count
        enum status "pending/approved/rejected/expired/executed/failed"
        bigint decided_by FK
        datetime decided_at
        text decision_note
        datetime expires_at
        datetime executed_at
        text result_json
    }
```

### 6.2 表清单与说明

| # | 表名 | 说明 | 预估行数（演示数据） |
|---|---|---|---|
| 1 | `departments` | 部门 | 4 |
| 2 | `users` | 用户 | 8 |
| 3 | `ticket_categories` | 工单分类（故障/需求/咨询/变更/权限/其他） | 6 |
| 4 | `sla_policies` | SLA 策略，按优先级 | 4 |
| 5 | `tickets` | 工单主表 | 60 |
| 6 | `ticket_comments` | 工单回复/内部备注 | 150 |
| 7 | `ticket_logs` | 工单操作审计日志（**只增不改**） | 400 |
| 8 | `agent_conversations` | Agent 会话 | 10 |
| 9 | `agent_messages` | Agent 消息历史（用于多轮上下文） | 80 |
| 10 | `agent_runs` | 单次 Agent 运行记录 | 40 |
| 11 | `agent_run_steps` | 运行内每步明细（执行过程可视化数据源） | 300 |
| 12 | `agent_pending_actions` | HITL 待确认动作 | 10 |

### 6.3 关键设计决策

**① 为什么 `agent_run_steps` 单独存表，而不是塞 JSON 字段？**

因为前端的「执行过程时间线」需要回放：用户刷新页面后，仍要能看到"Agent 调用了什么工具、参数是什么、返回了什么"。存 JSON 会导致无法分页、无法按 step_type 统计、无法计算工具耗时分布。**这是"可观测性"的基础，也是面试时的加分细节。**

**② 为什么 `ticket_logs` 要记录 `trace_id`？**

当 Agent 批量改了 17 条工单，每条都会写一条 `ticket_log`。`trace_id` 关联到 `agent_runs.run_id`，用户点开任意一条工单的历史，能看到"这条变更来自哪次 AI 会话、是谁批准的"。**这是 AI 写入业务数据的可追溯闭环**——很多 Demo 项目缺这一环。

**③ 为什么不用软删除？**

工单业务中"删除"本身就应该用 `cancelled` 状态表达，物理删除会破坏审计链。全表不用 `deleted_at`，减少查询复杂度。

**④ 索引策略**

```sql
tickets:           idx(status, priority)  idx(department_id, status)
                   idx(assignee_id, status)  idx(created_at)
                   uk(ticket_no)
ticket_comments:   idx(ticket_id, created_at)
ticket_logs:       idx(ticket_id, created_at)  idx(trace_id)
agent_messages:    idx(conversation_id, created_at)
agent_run_steps:   idx(run_id, seq)
agent_runs:        uk(run_id)  idx(user_id, started_at)
agent_pending_actions: uk(action_id)  idx(status, expires_at)
```

工单列表页最高频的查询是"按状态 + 优先级筛选 + 按时间排序"，所以 `(status, priority)` 是首要复合索引。

**⑤ 时间字段统一 UTC 存储**，`DATETIME` 类型（不用 `TIMESTAMP`，避免 2038 问题），前端按 `Asia/Shanghai` 渲染。MySQL 连接串显式设置 `charset=utf8mb4` + `timezone=+00:00`。

---

## 7. API 模块划分

### 7.1 统一约定

- 前缀：`/api/v1`
- 认证：`Authorization: Bearer <jwt>`（**SSE 端点除外**，见 7.5）
- 分页：`?page=1&page_size=20`，响应 `{items, total, page, page_size}`
- 时间：ISO 8601 带时区，如 `2026-09-21T10:30:00Z`
- 错误响应体统一：

```json
{
  "code": "TICKET_INVALID_TRANSITION",
  "message": "工单已关闭，不能再次分派",
  "detail": {"ticket_no": "OPS-2026-000123", "from": "closed", "to": "processing"}
}
```

HTTP 状态码语义化：400 参数错 / 401 未认证 / 403 无权限 / 404 不存在 / 409 状态冲突 / 422 校验失败 / 500 服务端错误。

### 7.2 接口清单

#### A. 认证与用户 `/api/v1`

| 方法 | 路径 | 说明 | 角色 |
|---|---|---|---|
| POST | `/auth/login` | 登录，返回 access_token + 用户信息 | 公开 |
| POST | `/auth/logout` | 登出（Redis 黑名单 token，简化实现） | 登录 |
| GET | `/auth/me` | 当前用户信息 + 权限列表 | 登录 |
| GET | `/users` | 用户列表（按部门筛选） | admin/manager |
| POST | `/users` | 创建用户 | admin |
| PATCH | `/users/{id}` | 修改用户（角色/部门/启停用） | admin |
| GET | `/departments` | 部门列表 | 登录 |

#### B. 工单中心 `/api/v1/tickets`

| 方法 | 路径 | 说明 | 角色 |
|---|---|---|---|
| GET | `/tickets` | 列表，筛选：status/priority/category/department/assignee/keyword/时间范围 | 登录（按 scope 过滤） |
| POST | `/tickets` | 创建工单（自动计算 SLA） | operator+ |
| GET | `/tickets/{ticket_no}` | 详情（含评论、日志、SLA 剩余时间） | 登录 |
| PATCH | `/tickets/{ticket_no}` | 修改标题/描述/分类/优先级 | 有权限者 |
| POST | `/tickets/{ticket_no}/assign` | 分派/转派 | operator+ |
| POST | `/tickets/{ticket_no}/transition` | 状态流转（body: `{to_status, reason}`） | 按状态机 |
| POST | `/tickets/{ticket_no}/escalate` | 升级优先级 | operator+ |
| GET | `/tickets/{ticket_no}/comments` | 评论列表 | 登录 |
| POST | `/tickets/{ticket_no}/comments` | 添加评论/内部备注 | 登录 |
| GET | `/tickets/{ticket_no}/logs` | 操作日志 | 登录 |
| POST | `/tickets/batch/update` | **批量修改**（人工入口，走同一 Service） | manager/admin |
| GET | `/categories` | 分类列表 | 登录 |
| GET | `/sla/policies` | SLA 策略列表 | admin |
| PATCH | `/sla/policies/{id}` | 修改 SLA 策略 | admin |

> 注：`/tickets/batch/update` 是**人工的批量入口**，与 Agent 批量修改复用同一个 `TicketService.batch_update()`。前端按钮和 AI 走同一条路——这是 4.2 设计红线的具体体现。

#### C. Dashboard `/api/v1/dashboard`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/dashboard/overview` | 卡片数：总数/待分派/处理中/超期/今日新增/今日关闭 |
| GET | `/dashboard/trend` | 折线图：近 N 天新增 vs 关闭 |
| GET | `/dashboard/priority-distribution` | 饼图：优先级分布 |
| GET | `/dashboard/status-distribution` | 饼图：状态分布 |
| GET | `/dashboard/sla-risk` | SLA 风险清单（Top N 紧急工单） |
| GET | `/dashboard/workload` | 柱状图：各处理人在办工单数 |

#### D. Agent Copilot `/api/v1/agent`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/agent/conversations` | 会话列表 |
| POST | `/agent/conversations` | 新建会话 |
| GET | `/agent/conversations/{id}/messages` | 历史消息（用于刷新恢复） |
| DELETE | `/agent/conversations/{id}` | 删除会话 |
| **POST** | **`/agent/chat`** | **★ SSE 主入口**，body: `{conversation_id?, message}` |
| **POST** | **`/agent/actions/{action_id}/approve`** | **★ 确认执行**，可带 `edited_payload`，返回 SSE |
| POST | `/agent/actions/{action_id}/reject` | 拒绝，body: `{reason?}` |
| GET | `/agent/actions/pending` | 当前用户待确认动作列表（前端红点提示） |
| GET | `/agent/runs/{run_id}` | 运行详情（含 steps，用于执行过程回放） |

### 7.3 两个 Agent 入口的对称性

```
POST /agent/chat                  → runner.start()  → SSE 流
POST /agent/actions/{id}/approve  → runner.resume() → SSE 流
```

两者返回**完全相同的 SSE 事件格式**，前端用同一个 `useAgentStream` composable 处理。区别只在于：前者创建新 run，后者恢复一个 `awaiting_approval` 的 run 并带着 `approval` 决策进入图。

### 7.4 为什么确认接口也要走 SSE？

因为批量执行 17 条工单是**多步操作**，用户需要看到"第 3 条执行失败、第 4 条成功"。如果返回一个普通 JSON，就只能等全部执行完才知道结果。走 SSE 可以逐条反馈，且第 3 条失败时能立刻提示。

### 7.5 SSE 认证方案

浏览器的 `EventSource` API **不支持自定义请求头**，无法携带 `Authorization: Bearer`。三种方案对比：

| 方案 | 做法 | 评价 |
|---|---|---|
| A. Token 放 Query | `?token=xxx` | token 会进 nginx access log，安全性差 |
| B. Cookie | 登录时同时下发 HttpOnly Cookie | 需要额外的 CSRF 防护 |
| **C. fetch + ReadableStream** | **用 fetch 发 POST，手动解析 SSE 流** | **✅ 采用** |

选 C 的原因：① 原生支持 `Authorization` 头；② 支持 POST（`EventSource` 只能 GET，聊天内容会被塞进 URL）；③ 支持 `AbortController` 取消；④ 可以自定义重连逻辑。代价是需要自己写约 60 行的 SSE 解析器（`frontend/src/api/sse.ts`），但这正好是一个可以讲的实现细节。

---

## 8. Agent 架构

### 8.1 设计原则

1. **单 Agent 单图**。不引入 Multi-Agent、Supervisor、DeepAgents——本项目规模下一个带工具的状态机已经足够，多 Agent 只会增加不可控性和调试难度。
2. **读工具直接执行，写工具分级管控**。避免"问个问题也要点确认"。
3. **Agent 不直连数据库**。所有工具调用 Service 层，复用权限与状态机。
4. **每一步可观测**。进入 `agent_run_steps`，前端可回放。
5. **循环有上限**。`step_count > 12` 强制进入 `respond` 节点收尾，杜绝无限工具调用。

### 8.2 图结构

```mermaid
flowchart TD
    START([用户输入]) --> U[understand<br/>意图识别 + 实体抽取]
    U --> ROUTE{intent 路由}

    ROUTE -->|chat / 无法识别| RESP[respond<br/>直接回答]
    ROUTE -->|create_ticket| PLAN
    ROUTE -->|query_ticket| PLAN
    ROUTE -->|analyze| PLAN
    ROUTE -->|batch_update| PLAN

    PLAN[plan<br/>LLM 决策下一步工具调用] --> NEED{需要调工具?}
    NEED -->|否| RESP
    NEED -->|是| POLICY[policy_check<br/>权限裁剪 + 风险判定]

    POLICY -->|拒绝| RESP
    POLICY -->|低风险，直接执行| ACT[act<br/>执行工具]
    POLICY -->|高风险，需确认| GUARD[guard<br/>生成计划 + 写 pending_action]

    ACT --> OBS[观测结果回灌 messages]
    OBS --> LIMIT{step_count > 12?}
    LIMIT -->|否| PLAN
    LIMIT -->|是| RESP

    GUARD --> INTERRUPT[[interrupt<br/>图挂起，SSE 推送 plan 事件]]
    INTERRUPT -.->|人工确认后 resume| EXEC[execute<br/>事务内批量执行]
    EXEC --> RESP

    RESP --> END([输出回答 + done])

    style GUARD fill:#ffe6cc
    style INTERRUPT fill:#ffcccc
    style EXEC fill:#d5e8d4
```

### 8.3 节点职责

| 节点 | 职责 | 关键实现 |
|---|---|---|
| `understand` | 意图分类 + 实体抽取 | `.with_structured_output(IntentResult)`，强制返回枚举 + 槽位 dict |
| `plan` | ReAct 决策：调什么工具、参数是什么 | `llm.bind_tools(available_tools)`，输出 `AIMessage.tool_calls` |
| `policy_check` | 权限裁剪 + 风险分级 + 是否需确认 | 纯 Python 逻辑，**不调用 LLM**（安全决策不能交给模型） |
| `act` | 执行单个低风险工具 | 参数 Pydantic 二次校验 → 调 Service → 结果截断后回灌 |
| `guard` | 生成结构化 Plan，写 `agent_pending_actions` | 计算 `affected_count`、`risk_level`，然后 `interrupt()` |
| `execute` | 审批通过后批量执行 | 单事务、逐条 SSE 推送、幂等键防重复 |
| `respond` | 生成最终自然语言回答 | 流式输出，携带 `citations`（引用工单号） |

**`policy_check` 刻意不用 LLM**：安全边界必须是确定性的。权限判定、风险分级、是否需人工确认，全部是硬编码规则。这是可以明确讲出来的设计权衡。

### 8.4 意图定义

```python
class Intent(str, Enum):
    CREATE_TICKET = "create_ticket"      # 场景 1
    QUERY_TICKET  = "query_ticket"       # 场景 2
    ANALYZE       = "analyze"            # 场景 3（SLA 风险、统计）
    BATCH_UPDATE  = "batch_update"       # 场景 4（强制 HITL）
    CHAT          = "chat"               # 闲聊/兜底
```

### 8.5 意图 → 工具白名单

**这是权限控制的第一道闸门**：不同意图下暴露给 LLM 的工具集不同，从源头减少工具误选。

| intent | 可用工具 |
|---|---|
| `create_ticket` | `get_metadata`, `create_ticket` |
| `query_ticket` | `query_tickets`, `get_ticket`, `get_metadata` |
| `analyze` | `get_sla_risk_report`, `get_ticket_statistics`, `query_tickets` |
| `batch_update` | `query_tickets`, `get_metadata`, `batch_update_tickets`, `batch_assign_tickets` |
| `chat` | 无（直接回答） |

### 8.6 多轮对话与记忆

**不使用 RAG、不使用向量库**。上下文策略：

1. 会话历史存 `agent_messages`，每轮取**最近 10 条**注入
2. 历史消息超过 20 条时，用 LLM 生成一句话摘要替换更早的消息（滚动摘要）
3. 单轮内工具返回结果**截断到 2000 字符**，超长时只保留前 N 条 + 总数

> 明确不做：长期记忆、跨会话记忆、用户画像。本项目不需要。

### 8.7 工具调用循环的终止条件

| 条件 | 处理 |
|---|---|
| LLM 返回无 `tool_calls` | 正常结束 → `respond` |
| `step_count >= 12` | 强制 → `respond`，回答中说明"已达到最大执行步数" |
| 同一工具 + 相同参数重复调用 2 次 | 中断 → `respond`，提示"检测到重复调用" |
| 工具连续失败 3 次 | 中断 → `respond`，返回错误摘要 |
| 单次 run 累计 token > 50k | 中断 → `respond` |
| 超时（120 秒） | 图级 `asyncio.timeout` → 报错事件 |

### 8.8 HITL 的状态承载：Redis Checkpoint

```python
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

checkpointer = AsyncRedisSaver.from_conn_string(settings.REDIS_URL)
graph = builder.compile(checkpointer=checkpointer)

# 首次执行
config = {"configurable": {"thread_id": run_id}}
async for chunk in graph.astream(state, config, stream_mode="updates"):
    ...

# 恢复执行（用户确认后）
async for chunk in graph.astream(Command(resume=decision), config, stream_mode="updates"):
    ...
```

`thread_id = run_id`，一个 run 一个线程。图在 `guard` 节点 `interrupt()` 后状态自动落 Redis，HTTP 连接可安全关闭，用户可能几分钟后才点确认。

**降级方案**（若 `langgraph-checkpoint-redis` 版本不兼容）：改用**两阶段 HTTP 模式**——`guard` 节点不调用 `interrupt()`，而是把 Plan 存库并结束图；`/approve` 接口读取 Plan，构造 State 从 `execute` 节点重新起一个图。

```
主方案：单图 + interrupt + Redis Checkpoint   ← 状态连续，能保留完整上下文
降级方案：两次独立图调用，Plan 落库传递        ← 无额外依赖，稍简单但上下文需重建
```

两个方案的 **SSE 事件格式、前端、数据库表完全相同**，切换只影响 `app/agent/graph.py` 一个文件。

---

## 9. Agent State 设计

### 9.1 完整定义

```python
# app/agent/state.py

from typing import Annotated, Literal, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    """计划中的一步操作。"""
    seq: int = Field(description="步骤序号，从 1 开始")
    tool: str = Field(description="工具名")
    args: dict = Field(description="工具入参（已解析、已校验）")
    description: str = Field(description="给用户看的中文描述，如「将 17 条工单升级为高优先级」")
    risk_level: Literal["low", "medium", "high"]
    requires_approval: bool = Field(description="是否需要人工确认")
    affected_count: int = Field(default=0, description="影响行数")
    preview: list[dict] = Field(default_factory=list, description="受影响对象摘要，用于前端预览")


class IntentResult(BaseModel):
    """understand 节点的结构化输出。"""
    intent: Literal["create_ticket", "query_ticket", "analyze", "batch_update", "chat"]
    confidence: float = Field(ge=0, le=1)
    entities: dict = Field(default_factory=dict, description="抽取到的槽位")
    missing_slots: list[str] = Field(default_factory=list, description="缺失的必填槽位")
    reasoning: str = Field(description="判定理由，写入 step 供回放")


class ApprovalDecision(BaseModel):
    """人工审批结果，通过 Command(resume=...) 注入。"""
    action_id: str
    decision: Literal["approved", "rejected"]
    decided_by: int
    edited_payload: dict | None = None   # 允许用户改完再执行
    note: str | None = None


class AgentState(TypedDict):
    # ── 输入上下文（图开始时一次性注入）──
    run_id: str
    conversation_id: int
    user_id: int
    user_role: str
    user_department_id: int | None
    user_input: str

    # ── 理解结果 ──
    intent: str | None
    intent_confidence: float
    entities: dict
    missing_slots: list[str]

    # ── 执行过程 ──
    messages: Annotated[list[BaseMessage], add_messages]   # 含 AIMessage / ToolMessage
    available_tools: list[str]          # 由 intent 决定的白名单
    pending_tool_calls: list[dict]      # 本轮待执行
    plan: list[PlanStep] | None         # 高风险操作的完整计划
    pending_action_id: str | None       # 对应的 agent_pending_actions.action_id
    approval: ApprovalDecision | None   # 恢复时注入

    # ── 观测与统计 ──
    observations: list[dict]            # [{tool, args, ok, summary, row_count}]
    affected_ticket_ids: list[int]      # 本次 run 影响的工单
    step_count: int                     # 循环保护
    total_tokens: int

    # ── 输出 ──
    answer: str | None
    citations: list[dict]               # [{ticket_no, title}] 引用来源
    error: str | None
    status: str                         # running/awaiting_approval/success/rejected/failed
```

### 9.2 状态字段设计说明

| 字段 | 为什么需要 |
|---|---|
| `available_tools` | 在 `understand` 后由 intent 计算得出，写入 State 而非临时变量——这样 `plan` 节点和调试日志都能看到"这一轮模型实际能调哪些工具" |
| `observations` | 与 `messages` 中的 `ToolMessage` 互补：`messages` 给 LLM 看（可截断），`observations` 给前端展示和统计分析用（结构化、完整摘要） |
| `affected_ticket_ids` | 批量执行后写回，用于在 `respond` 中生成准确的"共影响 N 条"话术，也写进 `ticket_logs.trace_id` |
| `step_count` | **循环保护的关键**。LangGraph 本身不限制循环次数，必须自己计数 |
| `plan` + `pending_action_id` | HITL 的核心载体：Plan 存 State（图内）也存 DB（图外），两者通过 `action_id` 关联 |
| `approval` | 从"图外"注入"图内"的唯一通道，`interrupt()` 恢复时携带 |
| `citations` | 防幻觉：回答里提到的每个工单号都必须来自真实查询结果，前端可点击跳转 |

### 9.3 状态的生命周期

```
① 初始 State   ← runner.start() 构造，只填充「输入上下文」段
② understand   ← 填充 intent / entities / available_tools
③ plan ⇄ act   ← 循环填充 messages / observations / step_count（最多 12 轮）
④ guard        ← 填充 plan / pending_action_id，status = awaiting_approval，然后挂起
   ⏸ 状态持久化到 Redis（checkpoint），进程可重启
⑤ resume       ← 注入 approval，status 回到 running
⑥ execute      ← 填充 affected_ticket_ids
⑦ respond      ← 填充 answer / citations / status = success
```

每个节点执行完，都会向 `agent_run_steps` 写一行（`node` 字段记录节点名），并向 SSE 推一个事件。**状态流转 = 事件流 = 数据库记录**，三者一一对应。这是"可观测"的落地方式。

---

## 10. Tool 划分

### 10.1 工具清单

#### 读工具（无副作用，直接执行）

| 工具名 | 入参 | 返回 | 用于场景 |
|---|---|---|---|
| `query_tickets` | `status[]`, `priority[]`, `category_id`, `department_id`, `assignee_id`, `keyword`, `created_after`, `created_before`, `unhandled_hours`, `limit≤50`, `order_by` | 工单摘要列表 + `total` | 2, 3, 4 |
| `get_ticket` | `ticket_no` | 详情 + 最近 10 条评论 + 最近 20 条日志 | 2 |
| `get_sla_risk_report` | `days=7`, `department_id?`, `priority?` | 风险分布（on_track/at_risk/breached 计数）+ 超期 Top 10 清单 + 按分类聚合 | 3 |
| `get_ticket_statistics` | `dimension`（status/priority/category/department/assignee）, `days`, `department_id?` | 分组计数 | 2, 3 |
| `get_metadata` | `kind`（departments/categories/users/priorities） | 枚举与字典 | 所有 |

#### 写工具（有副作用，按风险分级）

| 工具名 | 入参 | 风险 | 需确认 | 用于场景 |
|---|---|---|---|---|
| `create_ticket` | `title`, `description`, `category_id?`, `priority?`, `assignee_department_id?` | low | ❌ | 1 |
| `add_comment` | `ticket_no`, `content`, `comment_type` | low | ❌ | — |
| `update_ticket` | `ticket_no`, `title?`, `description?`, `priority?`, `category_id?` | medium | 仅当改 priority | — |
| `assign_ticket` | `ticket_no`, `assignee_id?`, `department_id?` | medium | ❌ | — |
| `transition_ticket` | `ticket_no`, `to_status`, `reason` | medium | 仅当 `to_status in (closed, cancelled)` | — |
| `batch_update_tickets` | `ticket_ids[]≤100`, `priority?`, `category_id?`, `department_id?` | **high** | ✅ **强制** | 4 |
| `batch_assign_tickets` | `ticket_ids[]≤100`, `assignee_id?`, `department_id?` | **high** | ✅ **强制** | 4 |

> **不提供 `delete_ticket` 工具**。Agent 永远不能物理删除数据——这是硬性安全约束，也是"AI 不越界"的体现。

### 10.2 风险分级规则（`agent/policy.py`）

风险判定**完全由代码决定，不问 LLM**：

```python
def assess_risk(tool_name: str, args: dict) -> tuple[str, bool]:
    """返回 (risk_level, requires_approval)"""
    if tool_name in READ_TOOLS:
        return "low", False

    if tool_name in {"batch_update_tickets", "batch_assign_tickets"}:
        return "high", True                     # 无条件确认

    if tool_name == "create_ticket":
        return "low", False                     # 新增数据可撤销

    if tool_name == "transition_ticket":
        if args.get("to_status") in {"closed", "cancelled"}:
            return "high", True                 # 终态不可逆
        return "medium", False

    if tool_name == "update_ticket" and "priority" in args:
        level = args.get("priority")
        current = ...                            # 查当前值
        if _is_upgrade(current, level):
            return "high", True                 # 升级优先级需确认
        return "medium", False

    if tool_name in {"add_comment", "assign_ticket"}:
        return "medium", False

    return "high", True                          # 未知工具：默认最严格
```

**核心判据三条**：① 是否影响多行数据；② 是否不可逆（终态 / 优先级升级）；③ 是否越权。命中任意一条 → 必须人工确认。

### 10.3 工具实现规范

所有工具遵循同一模板：

```python
@tool
async def query_tickets(
    ctx: ToolContext,
    status: list[str] | None = None,
    priority: list[str] | None = None,
    ...
) -> dict:
    """查询工单列表。当用户想了解工单情况时使用。

    Args:
        status: 状态筛选，可选值 pending_assign/processing/pending/resolved/closed/cancelled
        unhandled_hours: 只返回超过 N 小时未处理的工单（"超过24小时未处理"用这个参数）
    """
    # 1. 数据范围裁剪（强制）
    scope = ctx.scope_for_role()
    # 2. 调用 Service（不碰 ORM）
    result = await ticket_service.query(ctx.db, scope=scope, **params)
    # 3. 截断 + 结构化输出
    return {
        "total": result.total,
        "items": [t.to_agent_summary() for t in result.items[:50]],
        "truncated": result.total > 50,
    }
```

**四条铁律**：

1. **`ctx` 是第一个参数**，携带 `db` / `user` / `role` / `run_id`，LLM 不可见也不可伪造
2. **docstring 就是 Prompt**。工具描述决定 LLM 选得对不对，必须写清"什么时候用"而不只是"是什么"
3. **返回 dict，不返回 ORM 对象**。截断 + 只保留 LLM 需要的字段，控制 token
4. **数据范围裁剪在工具内强制生效**。`engineer` 调 `query_tickets` 只能看到自己名下的工单——不是靠 Prompt 约束，是靠代码

### 10.4 工具错误处理

```
工具抛异常
  ├─ 参数校验失败（Pydantic）→ 返回 {"ok": false, "error": "参数错误: priority 必须是 low/medium/high"}
  │                             回灌给 LLM，让它自我修正重试（最多 2 次）
  ├─ 业务异常（InvalidTransition / NotFound）→ 返回 {"ok": false, "error": "工单已关闭"}
  │                             回灌给 LLM，让它调整策略或告知用户
  └─ 系统异常（DB 连接失败）→ 返回 {"ok": false, "error": "系统繁忙"}
                              回灌 1 次仍失败 → 终止循环，SSE 推 error 事件
```

**永远不要把 Python traceback 直接喂给 LLM**——那会导致模型陷入混乱。

---

## 11. SSE 事件设计

### 11.1 协议格式

遵循标准 SSE：

```
event: tool_call
id: 7
data: {"call_id":"c1","tool":"query_tickets","args":{...},"risk_level":"low"}

```

- `event:` 事件名（见下表）
- `id:` 单调递增序号（从 1 开始），支持 `Last-Event-ID` 断线续传
- `data:` JSON，**单行**（不换行，避免多行 data 拼接歧义）
- 每 15 秒发一次心跳注释行 `: ping`，防止代理超时断连

### 11.2 事件全表

| # | 事件名 | 触发时机 | data 关键字段 | 前端处理 |
|---|---|---|---|---|
| 1 | `run_started` | 图开始执行 | `run_id`, `conversation_id`, `thread_id` | 创建占位消息，显示"思考中" |
| 2 | `node_started` | 每个节点开始 | `node`（understand/plan/act/...） | 时间线追加一行（调试态可见） |
| 3 | `intent` | `understand` 完成 | `intent`, `confidence`, `entities`, `reasoning` | 顶部展示识别的意图标签 |
| 4 | `thought` | LLM 产出思考文本 | `delta` | 灰色斜体追加，模拟思考过程 |
| 5 | `tool_call` | 工具调用前 | `call_id`, `tool`, `args`, `risk_level`, `requires_approval` | 渲染工具卡片（加载态） |
| 6 | `tool_result` | 工具返回后 | `call_id`, `ok`, `summary`, `row_count`, `duration_ms`, `data_preview` | 卡片转为完成态，展示摘要 |
| 7 | **`plan`** | **需人工确认时** | `action_id`, `title`, `steps[]`, `affected_count`, `risk_level`, `expires_at`, `preview[]` | **★ 渲染确认卡片** |
| 8 | `awaiting_approval` | 图挂起前 | `action_id`, `timeout_seconds` | 显示倒计时，锁定输入框 |
| 9 | `action_executed` | 批量执行中每条 | `action_id`, `seq`, `ticket_no`, `ok`, `message` | 卡片内进度条 +1，逐条打勾/叉 |
| 10 | `action_finished` | 批量执行结束 | `action_id`, `succeeded`, `failed`, `skipped` | 展示汇总 |
| 11 | `token` | 最终回答流式 | `delta` | 打字机效果 |
| 12 | `citations` | 回答结束 | `items[]`（ticket_no + title） | 渲染可点击的引用工单 |
| 13 | `message` | 消息落库后 | `message_id`, `role`, `content`, `run_id` | 替换占位消息为持久化 ID |
| 14 | `error` | 任意阶段出错 | `code`, `message`, `recoverable`, `node` | 红色错误条，可重试则显示按钮 |
| 15 | `done` | 流结束（**必定发送**） | `run_id`, `status`, `duration_ms`, `usage` | 关闭连接，恢复输入框，刷新工单列表 |

### 11.3 一次场景 4 的完整事件序列

```mermaid
sequenceDiagram
    participant FE as 前端
    participant BE as /agent/chat
    participant G as Graph

    FE->>BE: POST (message: "把所有超过24小时未处理的...")
    BE-->>FE: run_started
    BE-->>FE: node_started(understand)
    BE-->>FE: intent{intent: batch_update, entities:{hours:24, target_priority:high, dept:技术部}}
    BE-->>FE: node_started(plan)
    BE-->>FE: tool_call{query_tickets, args:{unhandled_hours:24, status:[pending_assign]}}
    BE-->>FE: tool_result{ok:true, row_count:17, duration_ms:42}
    BE-->>FE: node_started(policy_check)
    BE-->>FE: plan{action_id:"act_9f3a", steps:[2步], affected_count:17, preview:[17条]}
    BE-->>FE: awaiting_approval{timeout_seconds:600}
    Note over G: interrupt() — 图挂起，状态存 Redis
    BE-->>FE: done{status:"awaiting_approval"}
    Note over FE: 用户看到确认卡片，浏览 17 条工单
    FE->>BE: POST /agent/actions/act_9f3a/approve
    BE-->>FE: run_started
    BE-->>FE: node_started(execute)
    loop 17 条工单
        BE-->>FE: action_executed{seq:1, ticket_no:"OPS-2026-000101", ok:true}
    end
    BE-->>FE: action_finished{succeeded:17, failed:0}
    BE-->>FE: node_started(respond)
    BE-->>FE: token{delta:"已"}{delta:"完成"}{delta:"升级"}
    BE-->>FE: citations[{OPS-2026-000101}, ...]
    BE-->>FE: message{message_id:88, role:assistant, content:"..."}
    BE-->>FE: done{status:"success", duration_ms:8420}
```

### 11.4 前端事件消费

```typescript
// composables/useAgentStream.ts （核心逻辑示意）
const HANDLERS: Record<string, (d: any) => void> = {
  run_started:      d => agentStore.beginRun(d),
  intent:           d => agentStore.setIntent(d),
  tool_call:        d => agentStore.pushToolCard(d),
  tool_result:      d => agentStore.resolveToolCard(d),
  plan:             d => agentStore.showConfirmCard(d),   // ★
  action_executed:  d => agentStore.tickProgress(d),
  token:            d => agentStore.appendToken(d.delta),
  citations:        d => agentStore.setCitations(d.items),
  error:            d => agentStore.setError(d),
  done:             d => agentStore.endRun(d),
}
```

**未知事件名一律忽略，不报错**。这样后端加新事件不会导致老前端崩溃——前向兼容。

### 11.5 断连与容错

| 场景 | 处理 |
|---|---|
| 用户关页面 / 网络断 | 后端 `await request.is_disconnected()` 每轮检查，主动中止图执行，run 标记 `cancelled` |
| `done` 事件没收到 | 前端 3 秒后自动重查 `GET /agent/runs/{run_id}` 同步最终状态 |
| 图执行中进程崩溃 | run 状态卡在 `running`；启动时扫描超 10 分钟的 `running` run，标记为 `failed` |
| 用户重复点"确认执行" | `approve` 接口用 `action_id` 做幂等：状态非 `pending` 直接返回 409 |
| 审批超时（10 分钟） | Scheduler 扫描 `expires_at < now()` 的 `pending` 动作，标记 `expired`，run 标 `cancelled` |

---

## 12. Human-in-the-loop 流程

### 12.1 设计目标

HITL 不是"弹个框问一下"，而是一个**有状态、可审计、可恢复、有超时、支持修改**的完整审批子系统。

### 12.2 触发条件

只有同时满足以下条件的操作才进入 HITL：

```
① 是写操作（有副作用）
② 且命中高风险规则（见 10.2）：
     - 批量操作（影响多行）
     - 进入终态（closed / cancelled）
     - 优先级升级
     - 关闭 / 取消工单
③ 且 当前用户的角色有权限（无权限直接拒绝，不进 HITL）
```

**批量操作与终态变更永远需要确认**——这是不可配置的硬规则。

### 12.3 状态机

```mermaid
stateDiagram-v2
    [*] --> pending: guard 节点写入
    pending --> approved: 用户点「确认执行」
    pending --> rejected: 用户点「拒绝」
    pending --> expired: 超过 10 分钟未操作
    approved --> executed: 事务执行完成
    approved --> failed: 执行中异常（部分成功已提交）
    rejected --> [*]
    expired --> [*]
    executed --> [*]
    failed --> [*]
```

对应的 `agent_runs.status`：`running → awaiting_approval → (success | rejected | failed | cancelled)`

### 12.4 完整时序

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant FE as 前端
    participant API as API
    participant G as LangGraph
    participant DB as MySQL
    participant R as Redis

    U->>FE: "把所有超过 24 小时未处理的工单升级..."
    FE->>API: POST /agent/chat
    API->>G: astream(state)
    G->>G: understand → plan → 调 query_tickets（只读，直接执行）
    G->>G: policy_check → 判定 high risk
    G->>DB: INSERT agent_pending_actions(pending, expires_at=now+10min)
    G->>R: 保存 checkpoint
    G-->>API: interrupt
    API-->>FE: SSE: plan + awaiting_approval + done
    Note over FE: 渲染确认卡片<br/>17 条工单逐条可勾选<br/>10 分钟倒计时
    U->>FE: 取消勾选其中 2 条，点「确认执行」
    FE->>API: POST /agent/actions/{id}/approve<br/>{edited_payload: {ticket_ids: [15 条]}}
    API->>DB: SELECT ... FOR UPDATE 校验状态仍为 pending
    API->>DB: UPDATE status=approved, decided_by, decided_at
    API->>G: astream(Command(resume=decision), config)
    G->>G: execute 节点
    loop 每条工单
        G->>DB: UPDATE ticket + INSERT ticket_log(trace_id=run_id)
        G-->>FE: SSE: action_executed
    end
    G->>DB: UPDATE agent_pending_actions(status=executed, result_json)
    G->>G: respond
    API-->>FE: SSE: token... + citations + done
    FE->>FE: 刷新工单列表
```

### 12.5 确认卡片的内容设计

前端 `ActionConfirmCard.vue` 展示：

```
┌────────────────────────────────────────────────────────┐
│ ⚠️  需要你确认的操作                    剩余 09:32      │
├────────────────────────────────────────────────────────┤
│ 将 17 条工单升级为「高优先级」，并分配给「技术部」        │
│                                                        │
│ 计划步骤                                                │
│  ① 批量修改优先级 → 高      影响 17 条                  │
│  ② 批量分派到 技术部        影响 17 条                  │
│                                                        │
│ 影响的工单（取消勾选可排除）                             │
│  ☑ OPS-2026-000101  支付网关超时        [高] 26h未处理  │
│  ☑ OPS-2026-000103  订单同步失败        [中] 31h未处理  │
│  ☐ OPS-2026-000107  报表导出异常        [低] 25h未处理  │
│  ... 共 17 条，已选 15 条                                │
│                                                        │
│  [ 拒绝 ]                          [ 确认执行 (15) ]   │
└────────────────────────────────────────────────────────┘
```

**关键细节**：
- 展示**具体工单列表**（不是"17 条工单"这种模糊描述），用户能发现问题
- 支持**逐条取消勾选**，通过 `edited_payload` 回传
- 倒计时明确，避免用户以为点了就永久等待
- 拒绝按钮同样显眼，不做"诱导确认"的暗黑模式

### 12.6 批量执行的原子性与幂等

```python
async def execute_plan(db, action: AgentPendingAction, decision: ApprovalDecision):
    ids = decision.edited_payload.get("ticket_ids", action.payload["ticket_ids"])
    succeeded, failed = [], []

    async with db.begin():                       # ① 单事务
        for ticket_id in ids:
            try:
                await ticket_service.batch_update(...)   # 内部再开 SAVEPOINT
                succeeded.append(ticket_id)
            except BizError as e:
                failed.append({"id": ticket_id, "error": str(e)})
            await emit_sse("action_executed", {...})     # ② 实时推送

    # ③ 幂等：action_id 唯一约束 + 状态机校验，重复提交直接 409
    # ④ 部分失败也提交成功部分，result_json 记录明细，前端展示失败清单
```

**为什么用单事务 + SAVEPOINT 而不是全成功或全回滚？**
17 条工单里如果有 1 条已关闭导致流转失败，不应该让另外 16 条也白改。业务上半成功的语义更合理，且 `result_json` 完整记录了哪条失败、为什么失败。

### 12.7 权限规则

| 规则 | 说明 |
|---|---|
| 谁能批 | `admin` / `manager` 可批任何人发起的；`operator` 只能批自己发起的 |
| 不能自批的例外 | 由 `admin` 发起的 `high` 风险操作，可由任意 `admin` 批准（不做双人复核，超出项目范围） |
| 审批与执行分离 | 批准接口只负责标记决策，实际执行在恢复的图中完成，避免"点了批准但执行失败却已记为已批准"的歧义 |
| 数据范围二次校验 | 执行时对每条工单**重新校验**当前用户的数据范围，防止"提交时有权、执行时已调岗"的窗口问题 |

---

## 13. 开发阶段

### Phase 0：项目骨架与部署闭环（0.5 天）

**目标**：`docker compose up` 能起全栈，`/health` 返回 ok，前端能访问到后端。

- 目录结构、`requirements.txt`、`package.json`
- `docker-compose.yml` + 两个 Dockerfile + nginx.conf
- FastAPI 应用骨架、CORS、全局异常处理、日志
- Alembic 初始化
- 前端 Vite 骨架 + Element Plus + Axios 封装 + 路由骨架

**验收**：`docker compose up -d` → 浏览器打开 `localhost`，看到登录页；`localhost/api/v1/health` 返回 `{"status":"ok"}`。

### Phase 1：用户与权限（1 天）

- `users` / `departments` 表 + 迁移
- bcrypt 密码 + JWT 签发/校验
- `get_current_user` / `require_roles` 依赖
- 登录页 + 路由守卫 + Pinia user store + 菜单按角色渲染
- seed 脚本：4 部门 / 8 用户（覆盖 4 种角色，密码统一 `ops123456`）

**验收**：四种角色登录后看到的菜单不同；`engineer` 直接访问 `/api/v1/users` 返回 403。

### Phase 2：工单核心（2 天）

- `tickets` / `ticket_categories` / `ticket_comments` / `ticket_logs` 表
- `TicketService` CRUD + `ticket_state.py` 状态机
- 工单列表（筛选/分页/排序）、详情、创建弹窗、评论、状态流转
- 日志自动写入（每次变更）
- `ticket_no` 生成器（`OPS-{year}-{6位序号}`，Redis INCR）

**验收**：完整跑通"创建 → 分派 → 处理 → 解决 → 关闭"；尝试非法流转返回 409 且给出明确错误。

### Phase 3：SLA 与 Dashboard（1.5 天）

- `sla_policies` 表 + 创建时计算 `due_at`
- APScheduler 每 5 分钟刷新 `sla_status`
- 6 个 Dashboard 接口 + ECharts 图表
- Dashboard 页（卡片 + 折线 + 饼图 + 柱状 + 风险清单表格）

**验收**：手动把某工单 `created_at` 改到 3 天前，5 分钟后 `sla_status` 变为 `breached`。

### Phase 4：Agent 基础 —— 只读能力（2 天）

**这是 AI 部分的起点，先做只读，风险最低。**

- `llm.py`：ChatOpenAI 客户端（base_url 可配、超时、重试）
- `state.py`：AgentState 定义
- `graph.py`：StateGraph 骨架（understand → plan ⇄ act → respond）
- `understand` 节点：structured output 意图识别
- 读工具：`query_tickets` / `get_ticket` / `get_metadata`
- `events.py` + `runner.py`：图执行 → SSE 事件翻译
- `POST /agent/chat` SSE 端点
- 前端 `sse.ts` 解析器 + `useAgentStream` + 聊天面板 + 执行时间线
- `agent_runs` / `agent_run_steps` / `agent_messages` 落库

**验收（场景 2）**：输入"帮我看看最近有哪些高优先级工单没处理"，前端逐字显示思考过程，展示工具调用卡片，最终给出工单列表且可点击跳转。

### Phase 5：Agent 写能力 —— 单条操作（1.5 天）

- 写工具：`create_ticket` / `add_comment` / `assign_ticket` / `transition_ticket`
- 实体抽取强化：从"线上支付接口大量出现 502"提取标题、分类、优先级
- 槽位补全策略：缺失必填项时用分类默认值兜底，或在回答中追问
- 工具错误回灌与重试
- `citations` 引用渲染

**验收（场景 1）**：输入"线上支付接口大量出现 502，帮我报个故障"，成功创建工单并返回工单号，`tickets.source = 'agent'`，`ticket_logs.actor_type = 'agent'`。

### Phase 6：HITL —— 批量修改（2 天）

**本项目技术含量最高的阶段。**

- `agent_pending_actions` 表
- `policy.py`：风险分级 + 权限裁剪（纯代码，无 LLM）
- `batch_update_tickets` / `batch_assign_tickets` 工具
- `guard` 节点 + LangGraph `interrupt()` + Redis Checkpointer
- `execute` 节点：事务内批量执行 + 逐条 SSE
- `POST /agent/actions/{id}/approve|reject` 接口 + 幂等保护
- 审批超时清理任务
- 前端 `ActionConfirmCard.vue`：计划展示、逐条勾选、倒计时、进度条

**验收（场景 4）**：输入"把所有超过 24 小时未处理的工单升级成高优先级，并分配给技术部门" → 生成计划 → 看到 17 条工单 → 取消勾选 2 条 → 确认 → 逐条进度 → 数据库 15 条被改，`ticket_logs.trace_id` 关联到 run_id。**并且：中途重启后端容器，确认后仍能正确恢复执行**（验证 Redis checkpoint 生效）。

### Phase 7：分析能力与体验打磨（1.5 天）

- `get_sla_risk_report` / `get_ticket_statistics` 工具
- `analyze` 意图的 Prompt 优化，让输出包含结论 + 建议而不仅是数据
- 回答中嵌入 ECharts 图表（后端返回 chart spec，前端渲染）
- Copilot 侧边抽屉（在工单详情页可随时唤起）
- 滚动摘要（超 20 条历史时压缩）
- 空状态、加载态、错误态完善

**验收（场景 3）**：输入"分析最近 7 天的 SLA 风险" → 输出风险分布 + Top 10 超期工单 + 一段有洞察的结论（"技术部 breached 率 23%，主要集中在中优先级..."）。

### Phase 8：交付（1 天）

- `README.md`：项目简介、架构图、一键启动、4 个场景的 GIF 演示
- `scripts/seed.py` 完善演示数据（60 条工单，覆盖各状态/优先级/超期情况，时间分布合理）
- 后端 pytest 覆盖核心路径（目标 60%+，重点测 Service 层、状态机、工具层、HITL 恢复）
- `.env.example` 完整注释
- 录屏 + 截图

**总工期估算：约 13 天**（全职投入）。

### 里程碑检查点

| 里程碑 | 完成标志 | 可演示内容 |
|---|---|---|
| M1（Phase 0-1） | 能登录 | 部署闭环 |
| M2（Phase 2-3） | 工单系统可用 | 完整传统业务演示 |
| M3（Phase 4-5） | Agent 能读写单条 | 场景 1、2 |
| M4（Phase 6） | HITL 跑通 | **场景 4（核心亮点）** |
| M5（Phase 7-8） | 可交付 | 全部 4 个场景 + 文档 |

**M4 是最小可投递版本**——即使后面来不及，有 HITL 的项目也足够支撑简历。

---

## 14. 技术风险

### 14.1 高风险项

| # | 风险 | 影响 | 应对策略 |
|---|---|---|---|
| 1 | **LLM 不按格式返回**（结构化输出失败） | 意图识别崩溃，图无法继续 | 用 `with_structured_output` + Pydantic 强约束；解析失败重试 2 次；仍失败则降级为 `chat` 意图并友好告知 |
| 2 | **LLM 选错工具 / 编造参数** | 改错数据 | ① intent → 工具白名单收窄候选；② 工具内 Pydantic 二次校验；③ 高风险强制 HITL；④ 工具内部强制数据范围裁剪 |
| 3 | **SSE 被缓冲，前端一直空白** | Demo 直接失败，最打击信心 | nginx `proxy_buffering off` + 后端 `X-Accel-Buffering: no`；**开发阶段先用 `uvicorn --reload` 直连验证，再进 Docker** |
| 4 | **LangGraph `interrupt()` + Redis Checkpointer 版本不兼容** | HITL 无法实现 | 锁死 `langgraph` / `langgraph-checkpoint-redis` 版本；**Phase 6 第一天就做技术验证 spike**，不通立刻切两阶段 HTTP 降级方案（接口与前端不变） |
| 5 | **批量执行部分失败后状态不一致** | 数据脏 | 单事务 + SAVEPOINT；`result_json` 记录逐条结果；前端明确展示失败清单；不自动重试 |
| 6 | **Agent 越权操作** | 安全事故 | `policy.py` 纯代码判定，不看 LLM 脸色；执行时二次校验数据范围；不提供删除工具 |
| 7 | **重复提交导致重复修改** | 数据被改两次 | `agent_pending_actions.action_id` 唯一约束 + 状态机（仅 `pending` 可批）；`approve` 接口乐观锁（`SELECT ... FOR UPDATE`） |
| 8 | **工具调用死循环** | token 烧光、请求挂死 | `step_count > 12` 强制收尾；重复调用检测；单 run token 上限 50k；图级 120 秒超时 |

### 14.2 中风险项

| # | 风险 | 应对策略 |
|---|---|---|
| 9 | **SSE 长连接占满 worker** | 全链路 async；`uvicorn --workers 2`；SSE 路由单独限流（单用户最多 2 个并发流） |
| 10 | **MySQL 中文排序/时区错乱** | 连接串 `charset=utf8mb4` + `timezone=+00:00`；统一 UTC 存储，前端转本地时区；`collation=utf8mb4_unicode_ci` |
| 11 | **Token 成本失控** | 工具返回截断（≤2000 字符 / ≤50 行）；历史只取最近 10 条 + 滚动摘要；`agent_runs` 记录 token 用量可观测 |
| 12 | **演示环境无法访问外部 LLM** | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 全可配（OpenAI 兼容协议），可切本地 Ollama 或企业网关；提供 `MOCK_LLM=true` 离线演示模式 |
| 13 | **APScheduler 在多 worker 下重复执行** | 单 worker 跑 scheduler（`--workers 1`），或用 Redis 分布式锁；本项目选前者并在 README 说明 |
| 14 | **Alembic 迁移与模型不同步** | 每次改模型立刻 `alembic revision --autogenerate` + 人工 review；容器启动自动 `upgrade head` |
| 15 | **前端 TypeScript 类型与后端漂移** | `types/` 手写但严格对齐 `schemas/`；后端启用 OpenAPI，用 `/docs` 做人工核对 |

### 14.3 演示风险（容易被忽视但很致命）

| 风险 | 应对 |
|---|---|
| LLM 在演示时回答不稳定 | 提前录屏作为兜底；准备固定输入的"稳妥路径" |
| 演示数据太少，场景 4 只有 1 条工单可改 | seed 脚本保证至少有 15~20 条"超过 24 小时未处理"的工单 |
| 网络慢导致 SSE 卡顿 | 演示前预热一次请求；`LLM` 配置超时 30 秒 |
| 万一改错了数据 | 演示前 `mysqldump` 备份；提供 `seed.py --reset` 一键重置 |

---

## 15. 暂时不做的功能

### 15.1 明确排除的技术（用户约束）

| 技术 | 为什么不做 |
|---|---|
| **MCP** | 本项目工具是内部业务工具，不是通用能力；引入 MCP 会多一层进程/协议开销，且面试中"用了 MCP"不如"讲清楚 Tool Calling 的权限与风险分级"有说服力 |
| **Multi-Agent** | 四个场景都是单意图可完成的任务，Supervisor/Worker 模式只会增加不可控性和调试成本。单图足够，且更容易讲明白 |
| **DeepAgents** | 同上，且它是面向长周期开放任务的框架，与本项目"受控业务操作"的定位相悖 |
| **RAG** | 工单查询是**结构化精确查询**（状态/优先级/时间），SQL 是正确工具。相似工单推荐用 `LIKE` 关键字足够，不需要语义检索 |
| **向量数据库** | RAG 都不做，自然不需要。少一个中间件，少一份运维负担 |
| **微服务** | 单机单体是中小项目的正确形态。拆服务会带来分布式事务、服务发现、链路追踪等一堆与岗位无关的复杂度 |

### 15.2 暂时不做的业务功能

| 功能 | 不做的理由 | 如果要做 |
|---|---|---|
| 附件上传 | 需要对象存储（MinIO/OSS）+ 病毒扫描，与 AI 主线无关 | 后续用 MinIO + 简单类型/大小校验 |
| 邮件/短信通知 | 需要第三方服务与模板配置，纯成本 | 预留 `notifications` 表与事件钩子 |
| WebSocket 双向推送 | SSE 是单向的，已完全满足"推送执行过程"的需求 | 有实时协作编辑需求时再考虑 |
| Celery 异步队列 | 单实例下 APScheduler 足够；Celery 需要额外 worker 容器与 broker | 任务量上升或多实例部署时迁移 |
| 多租户 | 企业内网系统，无此需求 | 加 `tenant_id` 字段 + 全局过滤 |
| 国际化 i18n | 演示项目单一语言 | — |
| SSO / LDAP / OAuth | 本地账号密码已能演示权限体系 | 加 OAuth2 授权码流程 |
| 移动端适配 | 管理后台场景，桌面优先 | 响应式断点 |
| 工单满意度评价 | 与 AI 主线无关 | 加 `ticket_ratings` 表 |
| 知识库 / 常见问题自动回复 | 会滑向 RAG，刻意不做 | — |
| 富文本编辑器 | 纯 textarea 足够演示，省 1 天工期 | 引入 wangEditor |
| 复杂工作日历 SLA | 自然小时计算已能体现 SLA 逻辑 | 节日/排班表 + 工作时间窗口计算 |
| 可观测性平台（Jaeger/Prometheus） | 单机项目，结构化日志 + `agent_run_steps` 已足够 | 加 OpenTelemetry |
| K8s 部署 | Docker Compose 已满足"可部署"要求 | 写 Helm Chart |
| 后端 API 版本并存（v2） | 单版本，无历史包袱 | — |
| Agent 长期记忆 / 用户画像 | 无明确业务价值，且会增加隐私与合规复杂度 | — |
| 前端微前端 / 模块联邦 | 过度工程 | — |

### 15.3 一条底线

**不做"能跑但讲不清楚"的功能。**

每个写进代码的模块，都必须能回答：为什么需要它、不用它有什么问题、边界在哪里。宁可少做一个模块，也不留一个说不明白的黑盒——这是实习生项目与"抄来的 Demo"的分水岭。

---

## 附录 A：环境变量清单

```bash
# ── 应用 ──
APP_ENV=dev
APP_PORT=8000
SECRET_KEY=<random-32-bytes>
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# ── 数据库 ──
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_USER=opspilot
MYSQL_PASSWORD=opspilot123
MYSQL_DATABASE=opspilot
DATABASE_URL=mysql+asyncmy://opspilot:opspilot123@mysql:3306/opspilot?charset=utf8mb4

# ── Redis ──
REDIS_URL=redis://redis:6379/0

# ── LLM（OpenAI 兼容）──
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT=30
LLM_MAX_RETRIES=2
MOCK_LLM=false

# ── Agent 行为 ──
AGENT_MAX_STEPS=12
AGENT_MAX_TOKENS_PER_RUN=50000
AGENT_RUN_TIMEOUT_SECONDS=120
AGENT_BATCH_LIMIT=100
AGENT_APPROVAL_TIMEOUT_SECONDS=600

# ── SLA ──
SLA_SCAN_INTERVAL_MINUTES=5
```

## 附录 B：核心术语表

| 术语 | 含义 |
|---|---|
| **工单（Ticket）** | 一次待处理的服务请求，有状态机与 SLA |
| **SLA** | 服务级别协议，定义首响与解决的时限 |
| **SLA 风险** | `at_risk`（剩余时间 < 20%）或 `breached`（已超期） |
| **Copilot** | 面向用户的 AI 助手界面 |
| **Run** | 一次完整的 Agent 执行，`run_id` 是贯穿日志与审计的 trace 标识 |
| **Tool** | Agent 可调用的业务能力，是 Agent 与业务系统之间唯一的通道 |
| **Plan** | 高风险操作的结构化执行计划，需人工确认后才执行 |
| **HITL** | Human-in-the-loop，人在回路中审批 |
| **Pending Action** | 等待人工审批的动作记录 |
| **Policy** | 权限裁剪与风险分级规则，纯代码实现，不依赖 LLM |

---

**文档结束。本文档为架构冻结版，后续变更需同步更新版本号与变更记录。**
