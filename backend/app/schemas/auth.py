"""认证相关的出入参模型。

Schema 与 Model 分离的意义（这是面试常问的）：
    Model  = 数据【怎么存】，跟着数据库走
    Schema = 数据【怎么进出接口】，跟着 API 契约走

比如 User 表里有 password_hash 字段，但接口返回里【绝不能有】。
如果直接用 Model 当返回值，一不小心就把哈希串甚至密码泄露了。
分成两个类，返回什么由 Schema 决定，天然安全。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


# ══════════════════════════════════════════════════════════════
# 入参
# ══════════════════════════════════════════════════════════════


class RegisterRequest(BaseModel):
    """注册请求体。"""

    username: str = Field(
        min_length=3,
        max_length=50,
        description="登录名，3-50 字符",
        examples=["zhangsan"],
    )
    email: EmailStr = Field(
        description="邮箱，必须是合法邮箱格式",
        examples=["zhangsan@example.com"],
    )
    password: str = Field(
        min_length=6,
        # bcrypt 最多 72 字节，中文一个字 3 字节，所以给个保守上限
        max_length=72,
        description="密码，6-72 字符",
        examples=["ops123456"],
    )


class LoginRequest(BaseModel):
    """登录请求体。

    用 JSON body 而不是 OAuth2 的 form-data：
    FastAPI 教程里常见的是 form，但那是 OAuth2 规范的妥协。
    前后端都是我们自己写的，JSON 更自然，前端 axios 直接 post 对象。
    """

    # 这里叫 username 但实际允许填用户名【或】邮箱 —— 见 AuthService
    username: str = Field(description="用户名或邮箱", examples=["zhangsan"])
    password: str = Field(description="密码", examples=["ops123456"])


# ══════════════════════════════════════════════════════════════
# 出参
# ══════════════════════════════════════════════════════════════


class UserOut(BaseModel):
    """用户公开信息。

    ⚠️ 注意这里【没有 password_hash】。
    这就是 Schema 存在的意义 —— 返回什么由这个类决定，
    而不是由表里有什么决定。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """登录成功的响应。"""

    access_token: str = Field(description="访问令牌，后续请求放在 Authorization 头里")
    token_type: str = Field(default="bearer", description="令牌类型，固定是 bearer")
    expires_in: int = Field(description="有效期（秒）")
    user: UserOut = Field(description="当前登录用户信息")
