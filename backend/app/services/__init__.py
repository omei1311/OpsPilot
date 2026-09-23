"""业务服务层 —— 业务逻辑的唯一归属地。

这一层是人工入口（REST API）和 AI 入口（Agent Tool）的**共同底座**：

        REST API          Agent Tool
            │                  │
            └────────┬─────────┘
                     ▼
              services/  ← 权限校验 / 状态机 / 事务 / 审计
                     ▼
                 数据库

铁律：
  · Router 不写 SQL，Agent Tool 也不写 SQL
  · 事务边界在本层，由 Service 显式 await db.commit()
    （db/session.py 里的 get_session 不自动提交，就是为了把
      事务控制权留给这一层）
  · 本层不认识 HTTP，不 import Request / Response / HTTPException，
    只抛 app/core/exceptions.py 里的业务异常

后续阶段会补充：auth_service / user_service / ticket_service /
              ticket_state / sla_service / stats_service / audit_service
"""
