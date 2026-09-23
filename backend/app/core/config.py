"""应用配置。

所有可变参数集中在这里，通过环境变量 / .env 注入。
业务代码里【不要】出现硬编码的端口、密码、URL —— 一律从 settings 取。

用法::

    from app.core.config import settings
    print(settings.database_url)
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ★ .env 用【绝对路径】，不能用相对路径 ".env"。
#
# 相对路径是相对于【当前工作目录】解析的。这意味着：
#     从 backend/ 启动 uvicorn  → 找得到 backend/.env      ✅
#     从项目根目录跑脚本         → 找不到，静默用默认值      ❌
#
# 第二种情况的可怕之处是【不报错】—— 配置全变成默认值，
# 表现为"LLM_API_KEY 明明是配好的，程序却说没配"。
# 排查起来很费时间，因为文件明明就在那里。
#
# 这里用 __file__ 反推出 backend/ 目录，保证不管从哪执行都能找到。
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _BACKEND_DIR / ".env"


class Settings(BaseSettings):
    """从环境变量 / .env 加载的全局配置。

    命名约定：字段名大写，与 .env 中的 key 完全一致（大小写不敏感）。
    """

    model_config = SettingsConfigDict(
        # 绝对路径，不受当前工作目录影响。原因见文件顶部的说明。
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        # 允许 .env 里出现本类未声明的变量，不报错
        extra="ignore",
        case_sensitive=False,
    )

    # ── 应用 ────────────────────────────────────────────────
    APP_NAME: str = "OpsPilot"
    APP_VERSION: str = "0.1.0"
    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # ── 服务监听 ────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ── CORS ───────────────────────────────────────────────
    # 故意声明成 str 而不是 list[str]：
    # pydantic-settings 对复杂类型会尝试 JSON 解析，写成 "a,b" 会直接抛错。
    # 用逗号分隔的字符串 + 下面的属性方法，对使用者最友好。
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── MySQL ──────────────────────────────────────────────
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3307
    MYSQL_USER: str = "opspilot"
    MYSQL_PASSWORD: str = "opspilot123"
    MYSQL_DATABASE: str = "opspilot"
    DB_DRIVER: Literal["asyncmy", "aiomysql"] = "asyncmy"

    # 直接给完整连接串时，优先使用它，忽略上面的 MYSQL_* 组合
    DATABASE_URL: str | None = None

    # ── Redis ──────────────────────────────────────────────
    # 用途：JWT 黑名单（让登出真正生效）、Dashboard 统计缓存
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # ── 缓存 ───────────────────────────────────────────────
    # Dashboard 统计缓存的 TTL（秒）。
    # 60 秒 = 统计数字最多晚一分钟更新，业务完全可接受；
    # 同时它是失效竞态的兜底保险（见 core/cache.py 的注释），不能去掉。
    CACHE_DASHBOARD_TTL: int = 60

    # ── 连接池 ─────────────────────────────────────────────
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 3600
    DB_POOL_PRE_PING: bool = True

    # ── 日志 ───────────────────────────────────────────────
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # ── JWT ────────────────────────────────────────────────
    # 签名密钥。生产环境必须换掉 —— 泄露等于任何人都能伪造 token。
    # 生成方式：python -c "import secrets; print(secrets.token_urlsafe(32))"
    SECRET_KEY: str = "dev-only-secret-change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    # token 有效期（分钟）。1440 = 24 小时
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # ── 大模型（OpenAI 兼容接口）────────────────────────────
    # 只要服务商提供 OpenAI 兼容接口，改这两项就能切换：
    # 阿里云百炼 / DeepSeek / 智谱 / Kimi / 本地 Ollama 都行。
    LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # ⚠️ 默认值是占位符。真正使用时必须在 .env 里填自己的 Key。
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen-plus"
    LLM_TIMEOUT: int = 60
    LLM_MAX_RETRIES: int = 2
    # 温度设低：Agent 要稳定地调对工具，不需要创造力
    LLM_TEMPERATURE: float = 0.1

    # ── Agent 行为 ─────────────────────────────────────────
    AGENT_MAX_STEPS: int = 12
    AGENT_MAX_TOKENS_PER_RUN: int = 50000
    AGENT_RUN_TIMEOUT: int = 120
    AGENT_BATCH_LIMIT: int = 100
    AGENT_APPROVAL_TIMEOUT: int = 600

    @property
    def llm_configured(self) -> bool:
        """是否配置了可用的 API Key。

        Agent 相关接口启动时会检查这个值，没配就返回明确的提示，
        而不是让请求打出去等一个看不懂的 401。
        """
        key = (self.LLM_API_KEY or "").strip()
        return bool(key) and not key.startswith("sk-在这里填")

    # ── 派生属性 ────────────────────────────────────────────

    @property
    def cors_origins_list(self) -> list[str]:
        """把逗号分隔的 CORS_ORIGINS 拆成列表。"""
        raw = self.CORS_ORIGINS.strip()
        if not raw:
            return []
        # 兼容有人直接写成 JSON 数组的情况
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in raw.split(",") if item.strip()]

    @property
    def database_url(self) -> str:
        """异步数据库连接串。

        密码用 quote_plus 转义，避免密码里的 @ / : / # 把 URL 结构打乱 ——
        这是手拼连接串最常见的一个坑。
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL

        password = quote_plus(self.MYSQL_PASSWORD)
        return (
            f"mysql+{self.DB_DRIVER}://{self.MYSQL_USER}:{password}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
            f"?charset=utf8mb4"
        )

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV == "dev"

    @property
    def is_sqlite(self) -> bool:
        """是否使用 SQLite（测试环境可能用内存库）。

        SQLite 不支持连接池参数，建引擎时要区别对待。
        """
        return self.database_url.startswith("sqlite")

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带缓存的配置工厂。

    lru_cache 保证整个进程只解析一次 .env。
    测试里要换配置时用 get_settings.cache_clear() 再重新调用。
    """
    return Settings()


# 模块级单例，业务代码直接 import 这个
settings = get_settings()
