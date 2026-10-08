"""符号生成 Agent 的纯逻辑层。

这一层不依赖 LangGraph、不决定 I/O 去向 —— 便于单测，也便于拿 ``spike/``
里的历史产物做对拍回归（见 ``tests/test_symbol_contract.py``）。

模块来源分两类：

* **搬运**（源程序明文可读，逻辑未改）：``pkgname`` / ``pdf_locator`` /
  ``merge`` / ``selfcheck``；
* **重建**（源文件是 DLP 密文，按调用方契约复原）：``models`` / ``extract`` /
  ``layout``（对应源程序的 ``symbol.py``）。
"""

from backend.agents.symbol.contracts.extract import (
    Extraction,
    FigureInfo,
    extract_pins_from_table,
)
from backend.agents.symbol.contracts.layout import (
    PIN_LEN,
    PITCH,
    PlacedPin,
    SymbolLayout,
    build_layout,
    describe,
    preview,
)
from backend.agents.symbol.contracts.merge import (
    MergeResult,
    apply_resolution,
    merge_channels,
)
from backend.agents.symbol.contracts.models import (
    Conflict,
    DeviceSpec,
    Evidence,
    Pin,
    Side,
)
from backend.agents.symbol.contracts.pkgname import (
    PackageHint,
    hints_from_text,
    package_candidates,
    pin_count_from_package,
)

__all__ = [
    "PIN_LEN",
    "PITCH",
    "Conflict",
    "DeviceSpec",
    "Evidence",
    "Extraction",
    "FigureInfo",
    "MergeResult",
    "PackageHint",
    "Pin",
    "PlacedPin",
    "Side",
    "SymbolLayout",
    "apply_resolution",
    "build_layout",
    "describe",
    "extract_pins_from_table",
    "hints_from_text",
    "merge_channels",
    "package_candidates",
    "pin_count_from_package",
    "preview",
]
