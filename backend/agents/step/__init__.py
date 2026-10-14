"""STEP 数模生成助手。"""

from importlib import import_module

__all__ = [
    "build_drawing_to_step_graph",
    "build_image_to_step_graph",
    "get_postgres_step_snapshot",
    "invoke_postgres_step_graph",
    "open_postgres_step_graph",
    "select_graph_mode",
    "step_checkpoint_config",
]


def __getattr__(name: str):
    """Core CAD imports should not eagerly load the vision and database graph."""
    if name not in __all__:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module='graph' if name in {'build_drawing_to_step_graph','build_image_to_step_graph','select_graph_mode'} else 'persistence'
    value=getattr(import_module(f'{__name__}.{module}'),name)
    globals()[name]=value
    return value
