# backend/core/logger.py
# 全项目的日志工具：在标准库 logging 之上做一层包装，
# 支持「事件名 + 键值对」的结构化日志写法。

import logging  # Python 标准库的日志模块（你用过的那个）
import sys  # 用于把日志输出到标准输出 stdout
import os
from collections.abc import Iterable, Mapping
from typing import Any

# print(sys.path)
agents_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# print(f'agents_path-->{agents_path}')
sys.path.append(agents_path)
from backend.config import get_settings  # 读取配置（需要里面的 log_level 日志级别）


class _Logger:
    """日志包装类。
    作用：让我们能用 logger.info("事件名", key=value) 这种「结构化」写法，
    而不必每次手动拼字符串。内部仍委托给标准库 logging 真正输出。
    （前缀下划线 _ 表示它是「内部类」，外部应通过下面的 get_logger() 来获取实例）"""

    def __init__(self, name: str):
        # 构造方法：name 通常传 __name__（当前模块名），这样日志里能看出是哪个模块打的
        self._log = logging.getLogger(name)  # 拿到标准库的 logger 实例并存起来，后续都用它输出

    def _fmt(self, event: str, **kw) -> str:
        """内部方法：把「事件名 + 关键字参数」格式化成一行可读字符串。
        例：_fmt("user.login", user_id="u1") → "user.login | user_id='u1'"
        （**kw 表示接收任意多个 key=value 形式的关键字参数，收进一个字典 kw）"""
        if kw:  # 如果传了关键字参数（即 key=value）
            # 把每个键值对拼成 "key=value"（{v!r} 表示用 repr 显示值，字符串会带引号），
            # 再用空格连接，最前面加上事件名和分隔符 " | "
            # (user_id = "u1",role = "student")
            #  user.login |  user_id = "u1" role = "student"
            return event + " | " + " ".join(f"{k}={v!r}" for k, v in kw.items())
        return event  # 没有关键字参数时，只返回事件名本身

    def debug(self, event: str, **kw):
        """DEBUG 级别（最详细，调试用）"""
        self._log.debug(self._fmt(event, **kw))  # 先格式化，再交给标准库以 debug 级别输出

    def info(self, event: str, *args, **kw):
        """INFO 级别（常规信息）。
        兼容两种写法：① logger.info("事件", key=value)；② logger.info("模板 %s", 值)。
        （*args 接收任意多个「位置参数」，用于支持第二种老写法）"""
        if args:  # 如果传了位置参数，说明是「模板 + 值」的老写法
            self._log.info(event, *args)  # 直接交给标准库做 % 占位符格式化
        else:  # 否则按结构化写法处理
            self._log.info(self._fmt(event, **kw))

    def warning(self, event: str, *args, **kw):
        """WARNING 级别（警告：不影响运行但需注意）"""
        if args:
            self._log.warning(event, *args)
        else:
            self._log.warning(self._fmt(event, **kw))

    def error(self, event: str, **kw):
        """ERROR 级别（错误）。支持传 exc_info=True 把异常堆栈一并打印。"""
        # "计算错误 | operation="divide" "
        exc_info = kw.pop("exc_info", False)  # 从 kw 中取出 exc_info 参数（没有则默认 False），并移除它
        self._log.error(self._fmt(event, **kw), exc_info=exc_info)

    def critical(self, event: str, **kw):
        """CRITICAL 级别（严重错误）"""
        self._log.critical(self._fmt(event, **kw))

    def ocr_scan_result(
        self,
        *,
        scan_round: int | str,
        source: str,
        items: Iterable[Mapping[str, Any]],
        rotation_deg: int | None = None,
        elapsed_ms: float | None = None,
        event: str = "ocr.scan_completed",
    ) -> None:
        """按统一格式输出一轮 OCR 摘要及每条识别结果。

        Args:
            scan_round: 扫描轮次；实际 OCR 使用数字，缓存复用使用 ``cache``。
            source: 被扫描图片路径或缓存文件路径。
            items: 本轮识别结果，每项可包含 token_id、text、confidence、bbox、
                rotation_deg 和 accepted。
            rotation_deg: 本轮图片旋转角度；缓存结果可传 ``None``。
            elapsed_ms: 本轮耗时，缓存结果可不传。
            event: 摘要事件名，默认 ``ocr.scan_completed``。

        Returns:
            无返回值；摘要与明细均以 INFO 级别写入当前模块日志。
        """
        materialized = [dict(item) for item in items]
        accepted_count = sum(
            1 for item in materialized if item.get("accepted", True)
        )
        summary = {
            "scan_round": scan_round,
            "source": source,
            "rotation_deg": rotation_deg,
            "detected_count": len(materialized),
            "accepted_count": accepted_count,
        }
        if elapsed_ms is not None:
            summary["elapsed_ms"] = round(float(elapsed_ms), 2)
        self.info(event, **summary)
        for index, item in enumerate(materialized, start=1):
            self.info(
                "ocr.scan_item",
                scan_round=scan_round,
                item_index=index,
                token_id=item.get("token_id", f"scan_{scan_round}_{index:04d}"),
                text=str(item.get("text", "")),
                confidence=round(float(item.get("confidence", 0.0)), 6),
                bbox=item.get("bbox"),
                rotation_deg=item.get("rotation_deg", rotation_deg),
                accepted=bool(item.get("accepted", True)),
            )


def configure_logging() -> None:
    """全局日志配置函数：整个应用只在启动时（main.py）调用一次。
    负责设定日志格式、级别，并压低第三方库的噪音日志。"""
    settings = get_settings()  # 读配置，拿到 log_level（如 "INFO"）
    # getattr(logging, "INFO", 兜底) 把字符串级别转成 logging 模块里的常量 logging.INFO；
    # .upper() 先转大写；万一配置写错找不到该常量，就用 logging.INFO 兜底
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(  # 标准库的「一键全局配置」
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",  # 格式：时间 [级别] 模块名: 内容
        stream=sys.stdout,  # 把日志打到标准输出（控制台）
        level=level,  # 全局生效的日志级别
        force=True,  # 强制覆盖已有的日志配置（否则重复调用 basicConfig 会失效）
    )
    # 下面把几个第三方库的日志级别单独调到 WARNING，屏蔽它们大量的 INFO 噪音，避免刷屏
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)  # SQLAlchemy 的 SQL 执行日志
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)  # SQLAlchemy 连接池日志
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx HTTP 客户端日志
    logging.getLogger("httpcore").setLevel(logging.WARNING)  # httpcore 底层日志


def get_logger(name: str) -> _Logger:
    """对外的工厂函数：每个模块用 get_logger(__name__) 拿到自己的日志器。"""
    return _Logger(name)  # 返回一个 _Logger 实例（包装了标准库 logger）


configure_logging()  # 全局配置一次
if __name__ == '__main__':
    configure_logging()  # 全局配置一次
    log = get_logger("demo")
    # 如果消息后面传了关键字参数，会把关键字参数通过空格拼接在一起，形如 "user.login | user_id = "u1" role = "student""
    # log.info("user.login",user_id="u1",role="student")
    # log.info("user.login | user_id:%s, role:%s","u1","student")

    # log.info("user.login", user_id="u1", role="student")
    # log.warning("cache.miss", key="abc")
    try:
        result = 1 / 0
    except Exception as e:
        log.error("计算错误", operation="divide", exc_info=True)
