# OpsPilot Backend

企业智能工单与运营协同平台 —— 后端服务。

当前进度：**阶段 1 / 基础设施**（FastAPI + SQLAlchemy 2.x + MySQL + Alembic）。
业务模块（用户、工单、Agent）尚未实现，架构设计见仓库根目录 `PROJECT_ARCHITECTURE.md`。

---

## 技术栈

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.11+（开发用 3.12） | 用了 `X \| None` 等新语法 |
| FastAPI | 0.141 | Web 框架 |
| SQLAlchemy | 2.0 | ORM，`select()` 风格 + 异步 |
| Alembic | 1.20 | 数据库迁移 |
| MySQL | 8.0 | 业务数据库 |
| asyncmy | 0.2 | MySQL 异步驱动 |
| Pydantic Settings | 2.x | 配置管理 |

---

## 快速开始

### 1. 启动 MySQL

```bash
# 在仓库根目录执行
docker compose -f docker-compose.dev.yml up -d
```

> 映射到宿主机 **3307** 端口（不是 3306），避免和本机已安装的 MySQL 冲突。

确认就绪：

```bash
docker compose -f docker-compose.dev.yml ps
# STATUS 应显示 (healthy)
```

### 2. 创建虚拟环境并安装依赖

```bash
cd backend

# Windows
py -3.12 -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

默认配置已经能直接连上第 1 步起的 MySQL，无需修改。

### 4. 执行数据库迁移

```bash
alembic upgrade head
```

### 5. 启动服务

```bash
uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/docs> 查看接口文档。

---

## 验证

### 冒烟测试（推荐）

对着真实运行的服务打一遍所有关键接口，覆盖健康检查、错误格式、CORS、接口文档：

```bash
# 另开一个终端启动服务后执行
python scripts/smoke_test.py
```

### 单元测试

```bash
pytest -v
pytest -v -m no_db      # 只跑不依赖数据库的用例
```

数据库没启动时，依赖数据库的用例会自动 skip 而不是失败。

### 手动检查

```bash
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:8000/api/v1/health/db
```

---

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 服务信息与入口指引 |
| GET | `/api/v1/health` | 存活检查，不访问数据库 |
| GET | `/api/v1/health/db` | 数据库连通性 + 连接池状态 |
| GET | `/docs` | Swagger UI |
| GET | `/api/v1/openapi.json` | OpenAPI schema |

### 为什么健康检查拆成两个

- `/health` 是 **liveness**：进程还活着吗？**故意不注入 DbSession** ——
  一旦注入，数据库挂掉时连"进程还活着"这个信号都发不出去，
  编排系统会误判并疯狂重启，反而放大故障。
- `/health/db` 是 **readiness**：依赖就绪了吗？真的去查一次库，失败返回 503。

### 统一错误格式

任何错误响应都是同一个结构：

```json
{
  "code": "ROUTE_NOT_FOUND",
  "message": "Not Found",
  "detail": null
}
```

`code` 是机器可读的稳定标识，前端据此做分支；`message` 可直接展示给用户。
连 FastAPI 默认的 404 / 405 / 422 响应也被接管成了这个格式。

---

## 目录结构

```
backend/
├── app/
│   ├── main.py               FastAPI 实例、中间件、lifespan、路由挂载
│   ├── core/                 基础设施（不依赖任何业务模块）
│   │   ├── config.py         配置中心，所有环境变量在此声明
│   │   ├── logging.py        日志初始化 + UTF-8 输出
│   │   ├── exceptions.py     异常体系 + 全局异常处理器
│   │   └── deps.py           依赖注入定义（DbSession 等类型别名）
│   ├── db/
│   │   ├── session.py        异步引擎 + Session 工厂 + get_session 依赖
│   │   └── base.py           DeclarativeBase + 公共 Mixin
│   ├── models/               SQLAlchemy ORM 模型（阶段 2 起填充）
│   ├── schemas/              Pydantic 出入参
│   ├── services/             业务逻辑（阶段 2 起填充）
│   └── api/v1/               HTTP 接口
│       ├── __init__.py       路由汇总
│       └── health.py         健康检查
├── alembic/                  迁移脚本
├── scripts/smoke_test.py     端到端冒烟测试
├── tests/                    pytest
├── alembic.ini               迁移配置（⚠️ 纯 ASCII，见下）
└── requirements.txt
```

### 分层依赖方向

```
api/  ──┐
        ├──> services/ ──> models/ ──> db/
agent/ ─┘
```

- **Router 不写 SQL**，只做参数校验 + 调用 Service
- **Service 是唯一放业务逻辑的地方**，不认识 HTTP
- `agent/`（阶段 5 加入）与 `api/` **互不依赖**，是两个平行入口共享同一个 Service 层
  —— 保证「人改的数据」和「AI 改的数据」在权限、状态机、审计日志上完全等价

---

## 常用命令

```bash
# 改完模型后生成迁移
alembic revision --autogenerate -m "add ticket table"

# 查看/回滚
alembic current
alembic history
alembic downgrade -1
alembic upgrade head

# 生成 SQL 而不执行（交给 DBA 审核）
alembic upgrade head --sql > migrate.sql

# 代码检查
ruff check .
ruff format .

# 停止 MySQL
docker compose -f docker-compose.dev.yml down      # 保留数据
docker compose -f docker-compose.dev.yml down -v   # 连数据一起删
```

---

## Windows 开发注意事项

这三个坑都实际踩过，记录下来省得重复排查。

### 1. `alembic.ini` 必须保持纯 ASCII

Alembic 用 `configparser` 以 `encoding="locale"` 读取该文件。中文 Windows 的
locale 编码是 GBK，文件里任何非 ASCII 字节都会在 Alembic 启动前就抛
`UnicodeDecodeError`。所以这个文件的注释全部写成英文，中文说明放在本 README 里。

### 2. 需要 `tzdata` 包

Windows 没有系统时区数据库，Python 的 `zoneinfo` 找不到 `Asia/Shanghai`，
表现为 Alembic 报 `Can't locate timezone: Asia/Shanghai`。
`requirements.txt` 里已包含 `tzdata`。

### 3. 日志中文乱码

Windows 控制台默认 GBK，日志里的中文会乱码。`app/core/logging.py` 已强制把
stdout/stderr 切到 UTF-8 —— 这不只是为了好看：GBK 编码不了的字符会让
`logging` 抛 `UnicodeEncodeError`，足以把一个正常请求打成 500。

若在旧版 conhost 中仍看到乱码，执行一次 `chcp 65001`，或改用 Windows Terminal。

---

## 下一步（阶段 2）

- `users` / `departments` 表与迁移
- bcrypt 密码哈希 + JWT 签发校验
- `get_current_user` / `require_roles` 权限依赖
- 登录接口与用户管理接口
