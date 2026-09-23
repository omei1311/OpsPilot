"""日志配置。

在应用启动时调用一次 setup_logging()，接管 root logger。

本阶段只做基础格式化；后面接入 Agent 时会在格式里加入 trace_id，
让「一次 Agent 运行」产生的所有日志能串起来。
"""

from __future__ import annotations

import logging
import sys

from app.core.config import settings

# 格式：时间 | 级别 | 模块 | 消息
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _force_utf8_streams() -> None:
    """把 stdout / stderr 强制切到 UTF-8。

    为什么必须做：Windows 控制台默认编码是 GBK(cp936)。
    日志里只要出现一个 GBK 表示不了的字符（罕见汉字、emoji、用户输入的内容），
    logging 在写流时就会抛 UnicodeEncodeError —— 这个异常发生在
    写日志的瞬间，可能直接把一个正常请求打成 500。

    强制 UTF-8 后不再有「编码不了」的字符，从根上消除这类崩溃。
    现代终端（Windows Terminal / PowerShell 7 / VS Code）默认按 UTF-8 解析，
    显示正常；若在旧版 conhost 看到乱码，执行一次 chcp 65001 即可。

    注意：Python 3.7+ 才有 reconfigure，且流被重定向到文件时该属性依然存在。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                # errors="replace" 兜底：万一仍有异常字节，用 ? 代替而不是抛异常
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # 某些被包装过的流不支持重配置，静默跳过，不影响主流程
                pass


def setup_logging() -> None:
    """初始化全局日志。

    会清空已有的 handler —— 避免 uvicorn --reload 多次执行 lifespan
    导致同一个日志被打印两遍、三遍。
    """
    _force_utf8_streams()

    level = logging.DEBUG if settings.DEBUG else getattr(logging, settings.LOG_LEVEL)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # ── 第三方库降噪 ────────────────────────────────────────
    # SQLAlchemy 的 engine 日志非常吵，只在显式开启 DB_ECHO 时才放出来
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )
    # 连接池内部心跳（每次 pre_ping 都会打一条），平时不需要看
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取 logger 的快捷方式，统一入口便于以后加 trace_id 过滤器。"""
    return logging.getLogger(name)
