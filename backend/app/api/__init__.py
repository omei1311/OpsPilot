"""HTTP 接口层。

Router 的职责被刻意限制为三件事：
    1. 声明路径 / 方法 / 响应模型（供 OpenAPI 生成文档）
    2. 通过依赖注入拿到 Session、当前用户、分页参数
    3. 调用 Service，把结果 return 出去

不做的三件事：不写 SQL、不写业务规则、不 try/except 吞异常
（业务异常往上抛给 app/core/exceptions.py 统一处理）。
"""
