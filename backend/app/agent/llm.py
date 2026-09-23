"""LLM 客户端工厂。

只做一件事：按配置造出一个 ChatOpenAI 实例。

为什么不直接在 graph.py 里 new 一个？
    因为懒加载。模块导入时不创建客户端，第一次真正要用时才创建。
    这样即使没配 API Key，应用也能正常启动（只是 Agent 接口报错），
    而不是整个后端起不来。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError


@lru_cache(maxsize=1)
def get_llm() -> ChatOpenAI:
    """获取 LLM 客户端（进程内单例）。

    用 lru_cache 是因为 ChatOpenAI 内部维护了 httpx 连接池，
    每次新建会浪费连接。整个进程共用一个就够了。

    Raises:
        ServiceUnavailableError: 没配置 API Key。
            返回明确的中文提示，而不是让请求打出去等一个 401。
    """
    if not settings.llm_configured:
        raise ServiceUnavailableError(
            "还没有配置大模型 API Key，Agent 功能不可用。"
            "请在 backend/.env 里设置 LLM_API_KEY 后重启后端。",
            detail={"config_key": "LLM_API_KEY"},
        )

    return ChatOpenAI(
        model=settings.LLM_MODEL,
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        temperature=settings.LLM_TEMPERATURE,
        timeout=settings.LLM_TIMEOUT,
        max_retries=settings.LLM_MAX_RETRIES,
    )


def reset_llm_cache() -> None:
    """清掉缓存，下次调用会重新创建客户端。

    改了 .env 里的模型配置后可以用它热切换，不用重启进程。
    """
    get_llm.cache_clear()
