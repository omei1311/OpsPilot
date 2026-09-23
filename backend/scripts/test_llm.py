"""LLM 连通性测试。

在写 Agent 之前先跑这个 —— 如果 Key / 地址 / 模型名有问题，
在这里就能看到明确报错，而不是等到 Agent 跑起来才一头雾水。

    python scripts/test_llm.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from app.core.config import settings  # noqa: E402

GREEN, RED, DIM, YELLOW, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[33m", "\033[0m"


async def main() -> int:
    print("=" * 60)
    print("LLM 配置")
    print("=" * 60)
    print(f"  BASE_URL : {settings.LLM_BASE_URL}")
    print(f"  MODEL    : {settings.LLM_MODEL}")
    key = settings.LLM_API_KEY or ""
    # 只显示前后各 4 位，中间打码 —— 避免 Key 被日志或截图泄露
    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "(未设置或过短)"
    print(f"  API_KEY  : {masked}")
    print()

    if not settings.llm_configured:
        print(f"{RED}✗ 没有配置有效的 API Key{RESET}")
        print(f"  请编辑 backend/.env，把 LLM_API_KEY 换成你的真实 Key")
        return 1

    print("=" * 60)
    print("测试 1：基础对话")
    print("=" * 60)

    llm = ChatOpenAI(
        model=settings.LLM_MODEL,
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        temperature=settings.LLM_TEMPERATURE,
        timeout=settings.LLM_TIMEOUT,
        max_retries=settings.LLM_MAX_RETRIES,
    )

    try:
        resp = await llm.ainvoke(
            [
                SystemMessage(content="你是一个简洁的助手，回答不超过 10 个字。"),
                HumanMessage(content="用一句话说明什么是工单系统"),
            ]
        )
        print(f"  {GREEN}✓ 调用成功{RESET}")
        print(f"  {DIM}回复: {resp.content}{RESET}")
        usage = getattr(resp, "usage_metadata", None)
        if usage:
            print(f"  {DIM}tokens: {usage}{RESET}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}✗ 调用失败{RESET}")
        print(f"  {type(exc).__name__}: {str(exc)[:500]}")
        print()
        print(f"{YELLOW}排查建议：{RESET}")
        print("  · 401/403 → Key 不对，或者百炼控制台里没有开通该模型")
        print("  · 404    → LLM_MODEL 名字写错了")
        print("  · 超时   → 网络问题，或者 BASE_URL 不对")
        return 1

    print()
    print("=" * 60)
    print("测试 2：结构化输出（Agent 的意图识别依赖它）")
    print("=" * 60)

    from pydantic import BaseModel, Field

    class Intent(BaseModel):
        """意图识别结果。"""

        intent: str = Field(description="意图，取值: create_ticket / query / analyze / chat")
        confidence: float = Field(description="置信度 0-1")
        entities: dict = Field(default_factory=dict, description="抽取到的关键信息")

    try:
        structured = llm.with_structured_output(Intent)
        result = await structured.ainvoke(
            [
                SystemMessage(content="判断用户意图并抽取关键信息。"),
                HumanMessage(content="线上支付接口大量出现 502，帮我报个故障。"),
            ]
        )
        print(f"  {GREEN}✓ 结构化输出成功{RESET}")
        print(f"  {DIM}intent={result.intent} confidence={result.confidence}{RESET}")
        print(f"  {DIM}entities={result.entities}{RESET}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}✗ 结构化输出失败{RESET}")
        print(f"  {type(exc).__name__}: {str(exc)[:400]}")
        print()
        print(f"{YELLOW}注意：结构化输出失败通常意味着该模型不支持 function calling。{RESET}")
        print("  qwen-plus / qwen-max / deepseek-chat / glm-4 都支持，")
        print("  如果用的是很小的本地模型，需要换一个。")
        return 1

    print()
    print("=" * 60)
    print("测试 3：工具调用（Agent 的核心能力）")
    print("=" * 60)

    from app.agent.tools import ALL_TOOLS

    try:
        bound = llm.bind_tools(ALL_TOOLS)
        resp = await bound.ainvoke(
            [
                SystemMessage(
                    content="你是工单系统助手。根据用户需求调用合适的工具。"
                    "今天日期是 2026-09-21。"
                ),
                HumanMessage(content="帮我看看有哪些超过 24 小时没处理的高优先级工单"),
            ]
        )
        tool_calls = getattr(resp, "tool_calls", None) or []
        if tool_calls:
            print(f"  {GREEN}✓ 模型选择了工具{RESET}")
            for tc in tool_calls:
                print(f"  {DIM}→ {tc['name']}({tc['args']}){RESET}")
        else:
            print(f"  {YELLOW}⚠ 模型没有调用工具{RESET}")
            print(f"  {DIM}回复: {str(resp.content)[:200]}{RESET}")
            print("  工具调用仍可用，但模型可能需要更明确的提示词。")
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}✗ 工具绑定失败{RESET}")
        print(f"  {type(exc).__name__}: {str(exc)[:400]}")
        return 1

    print()
    print("=" * 60)
    print(f"{GREEN}全部通过 —— LLM 可以正常使用，可以开始写 Agent 了{RESET}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
