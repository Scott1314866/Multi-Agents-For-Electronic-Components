"""Keep native OCR/CAD work off the API event loop with bounded concurrency."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from typing import Any, Callable


# Paddle and OpenCascade each use one consistent worker thread. Concurrent jobs
# can still persist checkpoints and receive HTTP requests while native work runs.
_VISION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="step-vision")
_CAD_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="step-cad")


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
                # A Python task cancellation cannot stop native code. Retain the
                # job lock until it finishes writing its artifacts before exiting.
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # Retrieve a native failure without replacing the cancellation
                # that the job owner must persist and propagate.
                if not future.cancelled():
                    future.exception()
                raise cancelled

        return run

    return decorate


vision_node = _worker_node(_VISION_EXECUTOR)
cad_node = _worker_node(_CAD_EXECUTOR)
