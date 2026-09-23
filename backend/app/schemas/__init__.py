"""Pydantic 出入参模型。

职责边界：
    schemas  —— 「数据长什么样」，负责校验与序列化，不含业务规则
    models   —— 「数据怎么存」，SQLAlchemy ORM 映射
    两者刻意分开：表结构改了不该自动改变对外接口契约。

后续阶段会补充：auth.py / user.py / ticket.py / dashboard.py / agent.py
"""
