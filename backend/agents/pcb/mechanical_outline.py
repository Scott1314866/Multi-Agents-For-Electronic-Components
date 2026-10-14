"""Deterministic MAX terminal projection, separate from recommended lands."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class OutlineDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    minimum: float | None = Field(default=None, ge=0)
    maximum: float | None = Field(default=None, gt=0)
    typical: float | None = Field(default=None, gt=0)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("尺寸表 MIN 不能大于 MAX")
        return self


class OutlineTerminal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    side: Literal["top", "bottom"]
    pitch_index: float = Field(description="Observed offset in pitch units; e.g. -1,0,1, never pixel millimeters")
    width_symbol: str
    length_symbol: str | None = Field(default=None, description="Labeled flat contact length applying to THIS terminal, not a sloped segment")
    number: str | None = None
    number_source: Literal["visible", "unknown"] = "unknown"
    number_evidence: str = ""

    @model_validator(mode="after")
    def visible_number_only(self):
        if self.number_source != "visible" or not self.number_evidence.strip():
            self.number = None
        return self


class MechanicalOutlineReading(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    dimensions: dict[str, OutlineDimension]
    body_x_symbol: str
    body_y_symbol: str
    overall_y_symbol: str
    height_symbol: str = "A"
    pitch_symbol: str = "e"
    terminals: list[OutlineTerminal] = Field(min_length=2, max_length=256)
    source_units: Literal["mm", "inch", "mil", "unknown"]
    view: Literal["top", "bottom", "unknown"] = "unknown"
    view_evidence: str = ""
    rotation_deg: Literal[0, 90, -90, 180] = Field(default=0, description="Rotate image-plane coordinates only when the user's requested row orientation requires it")
    centered_rows: bool = Field(description="Rows symmetric about body center along lead-span direction")
    evidence: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode='before')
    @classmethod
    def normalize_value_metadata(cls, value):
        if not isinstance(value,dict):return value
        value=dict(value)
        if isinstance(value.get('dimensions'),dict) and isinstance(value.get('terminals'),list):
            value.pop('$schema',None)
            value['dimensions']={k:dict(v) if isinstance(v,dict) else v for k,v in value['dimensions'].items()}
            for symbol,entry in value['dimensions'].items():
                if isinstance(entry,dict) and not entry.get('evidence') and value.get('evidence'):
                    values={k:entry[k] for k in ('minimum','maximum','typical') if entry.get(k) is not None}
                    entry['evidence']=str(value['evidence'])+f'; structured table read {symbol}: {values}'
        return value

    @model_validator(mode='after')
    def explicit_view_proof(self):
        if self.view!='unknown' and not self.view_evidence.strip():self.view='unknown'
        return self

    @field_validator('view', mode='before')
    @classmethod
    def ambiguous_view(cls, value):
        return 'unknown' if value in (None, 'front', 'side', 'rear', 'plan') else value


def derive_max_outline(outline: MechanicalOutlineReading) -> tuple[dict, str]:
    """Project MAX contacts with zero solder allowance; unknown IDs stay null.

    Without a labeled contact length use (overall MAX - body MAX)/2 as
    an explicit projection rule. This is not an IPC tolerance calculation.
    """
    factor = {"mm": 1., "inch": 25.4, "mil": .0254}.get(outline.source_units)
    if factor is None:
        raise ValueError("MAX 派生需要明确尺寸表单位")
    if not outline.centered_rows:
        raise ValueError("两排相对本体中心不对称，需补充各侧位置尺寸")
    def dimension(symbol, *, pitch=False):
        entry = outline.dimensions.get(symbol)
        value = (entry.typical if pitch and entry.typical is not None else entry.maximum) if entry else None
        if value is None:
            raise ValueError(f"MAX 派生缺少 {symbol} 的" + ("TYP/BSC 或 MAX" if pitch else "MAX"))
        return value * factor
    body_x, body_y = dimension(outline.body_x_symbol), dimension(outline.body_y_symbol)
    span = dimension(outline.overall_y_symbol)
    height, pitch = dimension(outline.height_symbol), dimension(outline.pitch_symbol, pitch=True)
    if span <= body_y:
        raise ValueError("引脚外缘跨距 MAX 必须大于本体 MAX；请核对 D/E/E1 的实际指向")
    pads, formulas, occupied = [], [], set()
    for terminal in outline.terminals:
        key = (terminal.side, terminal.pitch_index)
        if key in occupied:
            raise ValueError("外形端子位置重复")
        occupied.add(key)
        width = dimension(terminal.width_symbol)
        length = dimension(terminal.length_symbol) if terminal.length_symbol else (span - body_y) / 2
        if length >= span / 2:
            raise ValueError("端子长度与外缘跨距不一致，请核对接触长度标注")
        x = terminal.pitch_index * pitch
        y = (span - length) / 2 * (1 if terminal.side == "top" else -1)
        formulas.append(f"{terminal.side}[{terminal.pitch_index:g}]: width={terminal.width_symbol}.MAX={width:g}; "
                        + (f"length={terminal.length_symbol}.MAX={length:g}" if terminal.length_symbol else
                           f"length=({outline.overall_y_symbol}.MAX-{outline.body_y_symbol}.MAX)/2={length:g} (projection rule)")
                        + f"; x=index*{outline.pitch_symbol}={x:g}; y=±(span-length)/2={y:g}")
        if outline.rotation_deg in (90, -90):
            x, y = (-y, x) if outline.rotation_deg == 90 else (y, -x)
            width, length = length, width
        elif outline.rotation_deg == 180:
            x, y = -x, -y
        pads.append(dict(number=terminal.number, x=round(x, 6), y=round(y, 6),
                         size_x=round(width, 6), size_y=round(length, 6), shape="RECTANGLE"))
    if outline.rotation_deg in (90, -90):
        body_x, body_y = body_y, body_x
    spec = dict(body_x=body_x, body_y=body_y, body_height_max=height, pin_layout="EXPLICIT",
                pin_count=len(pads), pads=pads, pin_numbering=[p['number'] for p in pads])
    audit = "MAX terminal projection; zero added toe/heel/side allowance; source view=" + outline.view + f"; rotation={outline.rotation_deg}; " + "; ".join(formulas)
    return spec, audit
