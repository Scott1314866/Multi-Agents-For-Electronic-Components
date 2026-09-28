"""兼容已有启动命令，复用标准 STEP 应用：python -m backend.new_main。"""

from backend.step_main import app, lifespan, run_server

__all__ = ["app", "lifespan", "run_server"]


if __name__ == "__main__":
    run_server()
