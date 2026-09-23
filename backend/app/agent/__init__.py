"""Agent 层。

职责：
    context.py  运行时上下文（身份从后端注入，不接受 LLM 传参）
    policy.py   风险分级（纯代码，不问模型）
    tools/      工具实现
    llm.py      LLM 客户端（阶段 9 加入）
    graph.py    状态图（阶段 9 加入）

分层的意义（架构文档 §4.2 的设计红线）：

        REST API          Agent Tool
            │                  │
            └────────┬─────────┘
                     ▼
              services/  ← 权限 / 状态机 / 事务 / 审计
                     ▼
                  数据库

Agent 层和 API 层是【两个平行入口】，共享同一个 Service 层。
所以本目录下的代码【不允许直接访问数据库】——
不写 SQL、不碰 ORM 模型，只能调 Service。
"""
