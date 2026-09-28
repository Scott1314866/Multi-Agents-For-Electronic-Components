"""标准后端入口：python -m backend.main。"""

from backend.step_main import app, lifespan, run_server

__all__ = ["app", "lifespan", "run_server"]


if __name__ == "__main__":
    run_server()
