"""把会阻塞的原生工作挪出事件循环。

三类活儿会长时间占住线程：

* **渲染**（PyMuPDF，CPU 密集）；
* **联网**（MinerU 上传/轮询、视觉模型，等待以分钟计）；
* **子进程**（Cadence ``tclsh.exe``）。

各自一条专属线程。单 worker 是刻意的：Caputre 的 X11/DBO 会话与工作目录
都是进程级共享，并行两个实例会互相污染。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from typing import Any, Callable

# 渲染（PyMuPDF）与联网（MinerU / 视觉）分别一条线程，互不阻塞。
_RENDER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="symbol-render")
_NETWORK_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="symbol-net")
# Cadence 的 DBO 会话与工作目录是进程级共享，只能串行。
_CAPTURE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="symbol-capture")


def _worker_node(executor: ThreadPoolExecutor) -> Callable:
    def decorate(function: Callable) -> Callable:
        @wraps(function)
        async def run(*args: Any, **kwargs: Any) -> Any:
            future = asyncio.get_running_loop().run_in_executor(
                executor, partial(function, *args, **kwargs)
            )
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError as cancelled:
                # Python 的取消停不下原生代码。等它把产物写完再退出，
                # 免得留下半截 .OLB / .DSN。
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not future.cancelled():
                    future.exception()
                raise cancelled

        return run

    return decorate


render_node = _worker_node(_RENDER_EXECUTOR)
network_node = _worker_node(_NETWORK_EXECUTOR)
capture_node = _worker_node(_CAPTURE_EXECUTOR)
