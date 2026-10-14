"""One FIFO native queue per configured Cadence device in this API process.

Symbol and PCB share these queues. Image/model work stays outside the queues;
native cancellation waits for the process to finish before releasing ownership.
Durable task and retry state remain in the existing PostgreSQL checkpoints.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

from backend.config import get_settings


def resolve_mode(kind: str, requested: str | None = None) -> str:
    settings = get_settings()
    mode = requested or (settings.pcb_execution_mode if kind == "pcb" else None) or settings.symbol_capture_mode
    if mode not in {"local", "ssh"}:
        raise ValueError("设备模式必须是 local 或 ssh")
    return mode


def device_key(mode: str) -> str:
    settings = get_settings()
    return "local" if mode == "local" else f"ssh:{settings.symbol_ssh_host.lower()}:{settings.symbol_ssh_port}"


class CadenceScheduler:
    def __init__(self):
        self._lock = threading.Lock()
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._entries: dict[str, dict] = {}

    async def run(self, function, *args, mode: str, kind: str, drawing_id: str = "", tenant_id: str = "", **kwargs):
        key = device_key(mode)
        ticket = uuid.uuid4().hex
        with self._lock:
            if key not in self._executors:
                self._executors[key] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cadence")
            executor = self._executors[key]
            self._entries[ticket] = {"mode": mode, "kind": kind, "drawing_id": drawing_id,
                                     "tenant_id": tenant_id, "state": "queued", "device_key": key}
            def execute():
                with self._lock:
                    self._entries[ticket]["state"] = "running"
                try:
                    return function(*args, **kwargs)
                finally:
                    with self._lock:
                        self._entries.pop(ticket, None)
            # Submit under the same lock as registration to keep FIFO order.
            future = asyncio.wrap_future(executor.submit(execute))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancelled:
            # A cancelled API owner must not free the native slot while Capture
            # or Allegro is still writing. Its graph owner persists recovery state.
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

    def snapshot(self, tenant_id: str) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in entry.items() if k not in {"tenant_id", "device_key"}}
                    for entry in self._entries.values() if entry["tenant_id"] == tenant_id]


cadence_scheduler = CadenceScheduler()
