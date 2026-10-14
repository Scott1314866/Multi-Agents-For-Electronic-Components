"""Public device metadata shared by PCB and the unified workbench."""
from pathlib import Path
import shutil

from backend.config import get_settings
from backend.core.cadence_scheduler import cadence_scheduler, resolve_mode


def device_status(tenant_id: str, *, settings=None, defaults=None) -> dict:
    settings = settings or get_settings()
    configured = bool(settings.symbol_ssh_host and settings.symbol_ssh_user
                      and settings.symbol_ssh_private_key and Path(settings.symbol_ssh_private_key).expanduser().is_file()
                      and shutil.which("ssh") and shutil.which("scp")
                      and (not settings.symbol_ssh_known_hosts or Path(settings.symbol_ssh_known_hosts).expanduser().is_file()))
    return {"items": [{"id": "local", "label": "本机 Cadence", "tools": ["symbol", "pcb"]},
                       {"id": "ssh", "label": "远程 Cadence", "tools": ["symbol", "pcb"], "configured": configured}],
            "defaults": defaults or {kind: resolve_mode(kind) for kind in ("symbol", "pcb")},
            "queues": cadence_scheduler.snapshot(tenant_id), "queue_scope": "backend_process",
            "status_note": "设备配置状态；实际连接、工具链及许可在运行时检查。"}
