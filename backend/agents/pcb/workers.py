"""把会阻塞的原生工作挪出事件循环。

Allegro 是子进程调用（本身已经释放 GIL），但 ``subprocess.run`` 的等待是
阻塞的，而且一次执行可能几分钟。放到专属线程里跑，其他任务仍能保存
checkpoint、响应 HTTP。

单 worker 是刻意的：Allegro 的 ``padpath``/``psmpath`` 与启动 cwd 强绑定，
同一时刻并行两个实例会互相污染工作目录里的 ``allegro.ilinit`` 与日志。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from typing import Any, Callable

# Allegro 一次只跑一个实例：cwd、padpath、日志文件都是进程级共享的。
_ALLEGRO_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pcb-allegro")


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
                # Python 的取消无法杀掉已经起来的原生进程。等它把产物写完
                # 再退出，免得留下半截 .dra / .psm。
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


cad_node = _worker_node(_ALLEGRO_EXECUTOR)
