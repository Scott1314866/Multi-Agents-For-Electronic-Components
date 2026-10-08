"""表格 HTML / 引脚图 → 结构化引脚（对应源程序的 ``app/extract.py``）。

源文件是密文。本实现依据 **Module1-Spec §11.5「表格解析硬化」** 全文重建 ——
那一节逐条记录了三种排版陷阱与对应修法，加上两处防误伤，共五条规则：

1. **表头不止一行** —— STM32 的表第二行才是封装名列（``BGA10 / LFP64 / LF10``）。
   只认第一行会让三列同名成 ``Pins``，无从分辨。
2. **列选择要三思** —— 三列同名时盲取第 0 列会取到 BGA 列，引脚号变成 ``A3``。
   三步优先级（不可换）：表头含目标名 → 目标名里的数字与某列最大编号吻合 →
   纯数字格子占比最高的列。
3. **中段会重印表头** —— MinerU 把跨页表格拼成一整块，每段重印表头
   （实测第 25/43/63/86 行各一次，且各段 OCR 结果不同，``LFP64`` 会变 ``LOFFP64``）。
   丢掉「与表头行撞上 ≥2 格」的行；用 ≥2 而非 ≥1，免得误杀
   「引脚号恰好等于表头文字」的行。
4. **``nc`` 不能当空占位符** —— NC 是"不连接"，但常常是个**有编号的真实引脚**，
   当成空会把整条引脚漏掉。
5. **引脚号要过两道闸** —— 形状像（纯数字或字母打头的短码如 ``A3``/``AB12``）
   **且**全 ASCII。ADS1115 的 NC 行里 DGS 那格本该是 ``-``、被 OCR 读成汉字
   「一」，没有这道闸就会多出一个编号为「一」的引脚。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re

from backend.agents.symbol.contracts.models import Pin, Side

#: 空占位符。**刻意不含 "nc"** —— NC 常是有编号的真实引脚。
_SKIP_CELLS = {"", "-", "—", "–", "n/a", "na", "/", "\\"}

#: 引脚号的合法形状：纯数字，或 1-3 个字母 + 1-3 位数字（BGA 的 A3 / AB12）。
_PIN_NUM_RE = re.compile(r"^\d+$|^[A-Za-z]{1,3}\d{1,3}$")

#: 表头关键词（小写）。任一中即认为该行是表头。
_HEADER_KEYWORDS = (
    "pin", "name", "symbol", "type", "description", "no.", "no ",
    "引脚", "名称", "编号", "类型", "描述", "功能",
)

#: 描述列的脚注后缀，如 "I²C target address select(1)"。
_FOOTNOTE_RE = re.compile(r"\s*\(\d+\)\s*$")


@dataclass
class Extraction:
    """一次表格解析的结果。"""

    pins: list[Pin] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 按列拼接后的表头文本（诊断用）。
    headers: list[str] = field(default_factory=list)
    #: 被选作引脚号的列下标。
    device_columns: list[int] = field(default_factory=list)


@dataclass
class FigureInfo:
    """引脚图（视觉通道）读出的**图形结构**。

    只负责"哪个引脚在哪条边上、那一条边上排第几"；引脚的名称、类型、描述
    一律由文本通道提供 —— 视觉模型读密集小字表格是幻觉高发区。
    """

    pin_sides: dict[str, list[str]] = field(default_factory=dict)
    page: int = 0
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def side_of(self, number: str) -> Side:
        """引脚在哪条边上。图上没有这个引脚时返回 ``unknown``（不抛异常）。"""
        for side, numbers in self.pin_sides.items():
            if number in numbers:
                return side  # type: ignore[return-value]
        return "unknown"

    def order_of(self, number: str) -> int:
        """在它所在的那条边里排第几（0 起）。找不到时返回 0。"""
        for numbers in self.pin_sides.values():
            if number in numbers:
                return list(numbers).index(number)
        return 0

    @classmethod
    def from_vision(cls, payload: dict, *, page: int = 0) -> "FigureInfo":
        """把视觉模型的 JSON 转成 :class:`FigureInfo`。

        接受 ``{"pins": [{"pin_number": "1", "side": "left"}, …]}`` 形式
        （与 :mod:`backend.agents.symbol.tools.review` 用的是同一套字段）。
        """
        sides: dict[str, list[str]] = {}
        warnings: list[str] = []
        for item in payload.get("pins") or []:
            number = str(item.get("pin_number") or "").strip()
            if not number:
                continue
            side = str(item.get("side") or "unknown").strip().lower()
            if side not in {"left", "right", "top", "bottom"}:
                warnings.append(f"引脚 {number} 的侧别未定")
                side = "unknown"
            sides.setdefault(side, []).append(number)
        return cls(pin_sides=sides, page=page, warnings=warnings, raw=payload)


# ── HTML 表格 → 网格 ──────────────────────────────────────────────


class _TableParser(HTMLParser):
    """把第一个 ``<table>`` 解析成 ``[[(text, rowspan, colspan), …], …]``。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []
        self._in_table = False
        self._in_cell = False
        self._row: list[tuple[str, int, int]] = []
        self._buffer: list[str] = []
        self._span = (1, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in {"td", "th"} and self._in_table:
            self._in_cell = True
            self._buffer = []
            attributes = dict(attrs)
            self._span = (
                int(attributes.get("rowspan") or 1),
                int(attributes.get("colspan") or 1),
            )

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell:
            text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            self._row.append((text, self._span[0], self._span[1]))
            self._in_cell = False
        elif tag == "tr" and self._in_table and self._row:
            self.rows.append(self._row)
            self._row = []
        elif tag == "table":
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._buffer.append(data)


def _expand_grid(rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """把带 rowspan/colspan 的稀疏行展开成满格网格。"""
    if not rows:
        return []
    occupied: dict[tuple[int, int], str] = {}
    max_column = 0
    for row_index, row in enumerate(rows):
        column = 0
        for text, rowspan, colspan in row:
            while (row_index, column) in occupied:
                column += 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    occupied[(row_index + dr, column + dc)] = text
            max_column = max(max_column, column + colspan)
            column += colspan
    return [
        [occupied.get((r, c), "") for c in range(max_column)] for r in range(len(rows))
    ]


# ── 规则 5：引脚号的两道闸 ────────────────────────────────────────


def _valid_pin_number(text: str) -> bool:
    """形状像引脚号 **且** 全 ASCII。"""
    candidate = (text or "").strip()
    if not _PIN_NUM_RE.match(candidate):
        return False
    # 汉字「一」能被 isdigit() 挡掉，但 "１２３" 这类全角数字挡不住，故加 isascii。
    return candidate.isascii()


def _is_number_cell(text: str) -> bool:
    return (text or "").strip().isdigit()


# ── 规则 1：表头识别 ──────────────────────────────────────────────


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _looks_like_header(cell: str) -> bool:
    normalized = _norm(cell)
    if not normalized:
        return False
    return any(keyword in normalized for keyword in _HEADER_KEYWORDS)


def _split_header_and_data(rows: list[list[str]]) -> tuple[list[str], int]:
    """返回（按列拼接的表头文本, 数据起始行号）。

    表头 = 命中关键词的那一行 **加上紧随其后的非数据行**（规则 1）——
    STM32 的封装名列正是第二行。
    """
    if not rows:
        return [], 0
    columns = max(len(row) for row in rows)
    header_rows = 0
    if any(_looks_like_header(cell) for cell in rows[0]):
        header_rows = 1
        for index in range(1, len(rows)):
            if any(_valid_pin_number(cell) for cell in rows[index]):
                break
            header_rows += 1
    headers: list[str] = []
    for column in range(columns):
        parts = [
            rows[r][column] if column < len(rows[r]) else "" for r in range(header_rows)
        ]
        headers.append("\n".join(part.strip() for part in parts if part.strip()))
    return headers, header_rows


def _data_rows(
    rows: list[list[str]], header_rows: int, headers: list[str]
) -> list[list[str]]:
    """丢掉与表头撞上 ≥2 格的行（规则 3：跨页重印的表头）。"""
    header_cells = {
        cell.strip()
        for row in rows[:header_rows]
        for cell in row
        if cell.strip()
    }
    kept: list[list[str]] = []
    for row in rows[header_rows:]:
        if not any(cell.strip() for cell in row):
            continue
        overlap = sum(1 for cell in row if cell.strip() and cell.strip() in header_cells)
        if overlap >= 2:
            continue
        kept.append(row)
    return kept


# ── 规则 2：列选择 ────────────────────────────────────────────────


def _column_hits(headers: list[str], keywords: tuple[str, ...]) -> list[int]:
    return [
        index
        for index, header in enumerate(headers)
        if header and any(keyword in _norm(header) for keyword in keywords)
    ]


def _number_columns(
    headers: list[str], data: list[list[str]], target: str
) -> list[int]:
    """选引脚号列。三步优先级**不可换**（规则 2）。"""
    columns = len(headers)

    # ① 表头含目标名（封装代码或型号）
    if target:
        wanted = _norm(target)
        hits = [i for i, header in enumerate(headers) if wanted and wanted in _norm(header)]
        if hits:
            return hits

    # ② 目标名里的数字与某列的最大编号吻合（LQFP100 → 100）
    match = re.search(r"(\d+)\s*$", target or "")
    if match:
        wanted_count = int(match.group(1))
        for column in range(columns):
            numbers = [
                int(row[column])
                for row in data
                if column < len(row) and _is_number_cell(row[column])
            ]
            if numbers and max(numbers) == wanted_count:
                return [column]

    # ③ 兜底：纯数字格子占比最高的列
    best: list[int] = []
    best_ratio = -1.0
    for column in range(columns):
        cells = [row[column] for row in data if column < len(row)]
        if not cells:
            continue
        ratio = sum(_is_number_cell(cell) for cell in cells) / len(cells)
        if ratio > best_ratio:
            best, best_ratio = [column], ratio
    return best


def _clean_description(text: str) -> str:
    return _FOOTNOTE_RE.sub("", text or "").strip()


# ── 主入口 ────────────────────────────────────────────────────────


def parse_table_html(html: str) -> list[list[str]]:
    """HTML 表格 → 展开后的二维网格。"""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return _expand_grid(parser.rows)


def extract_pins_from_table(
    html: str, *, page: int, device: str = ""
) -> Extraction:
    """表格 HTML → 引脚清单。

    Args:
        html: MinerU 输出的 ``table_body``。
        page: 该表所在的页码（写进证据）。
        device: 目标器件名或封装代码，用于挑列（``_number_columns`` 的 ① ②）。

    Returns:
        引脚清单与降级说明。解析不出任何引脚时返回空 pins 并附 warning ——
        「降级要有痕迹」，这是迁移必须继承的纪律之一。
    """
    rows = parse_table_html(html)
    if not rows:
        return Extraction(warnings=["表格里没有可解析的行"])

    headers, header_rows = _split_header_and_data(rows)
    data = _data_rows(rows, header_rows, headers)
    if not data:
        return Extraction(headers=headers, warnings=["表头之后没有数据行"])

    number_columns = _number_columns(headers, data, device)
    name_columns = _column_hits(headers, ("name", "symbol", "引脚名", "名称"))
    type_columns = _column_hits(headers, ("type", "类型"))
    description_columns = _column_hits(headers, ("description", "描述", "功能"))

    pins: list[Pin] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for row in data:
        number = ""
        for column in number_columns:
            if column < len(row) and _valid_pin_number(row[column]):
                number = row[column].strip()
                break
        if not number:
            # 有内容却给不出合法引脚号 —— 记一笔，不静默丢弃（规则 5）。
            filler = next(
                (cell.strip() for cell in row if cell.strip() and not _is_skip(cell)),
                "",
            )
            if filler:
                warnings.append(
                    f"第 {len(pins) + 1} 行「{filler}」给不出合法引脚号，已跳过"
                )
            continue
        if number in seen:
            warnings.append(f"引脚 {number} 重复出现，已去重")
            continue
        seen.add(number)

        name = _first_of(row, name_columns) or number
        pin_type = _pin_type(_first_of(row, type_columns))
        description = _clean_description(_first_of(row, description_columns))

        pins.append(
            Pin(
                pin_number=number,
                name=name,
                type=pin_type,
                description=description,
            )
        )

    if not pins:
        warnings.append("表里没有解析出任何合法引脚号")
    return Extraction(
        pins=pins, warnings=warnings, headers=headers, device_columns=number_columns
    )


def _is_skip(cell: str) -> bool:
    return cell.strip().lower() in _SKIP_CELLS


def _first_of(row: list[str], columns: list[int]) -> str:
    for column in columns:
        if column < len(row) and row[column].strip():
            return row[column].strip()
    return ""


def _pin_type(raw: str) -> str:
    """表格 TYPE 列的文本 → 短码。

    顺序要紧：``Digital I/O`` 含 ``i/o``，必须先判 IO，否则会被
    ``output`` / ``input`` 抢先。与六份 ``*_spec.json`` 的实测值域
    （IN / OUT / IO / PAS）对齐。
    """
    text = (raw or "").lower()
    if "i/o" in text or "input/output" in text or "bidir" in text:
        return "IO"
    if "output" in text:
        return "OUT"
    if "input" in text:
        return "IN"
    return "PAS"
