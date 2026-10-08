"""结果自检器：判断"这次生成的符号到底能不能用"（docs/specs/Module1-Spec.md §11.7）。

**为什么需要它**：⑤ 之后程序只知道 `capture.generate(...).passed` —— 那只能说明
TCL 跑通了、文件写出来了，**不代表符号是对的**。STM32F105 那单就是活例子：100 个
引脚全部侧别未定、被堆在符号左侧，符号等于废的，可 `CAPTURE RESULT: PASS`，
页面上写着"生成成功"。静默生成一个废符号，比报错更糟。

**设计约束**：这个模块是**纯函数、零 I/O** —— 不提问、不打印、不修改传进来的数据，
只产出 finding。提问和渲染交给调用方（`app/cli.py` / `web/service.py`），于是同一份
判断能用在三个地方：CLI 的确认闸之前、Web 的结果页、以及离线回归集（`tests/regress.py`）。

**将来**：agent 化要"知道哪次尝试更好"，靠的就是这里的 finding code + `score`。
所以 score 从一开始就做进去，哪怕现在没人比它。

用 `check()` 的姿势：**能给的参数就给，给不出的就跳过对应检查**。离线回归只给
`pins` / `package` / `device`，侧别类和生成类检查自然不跑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from .models import Conflict, Pin
from .pkgname import (
    PackageHint,
    _plausible_pin_count,
    describe_hints,
    hinted_counts,
    package_candidates,
    pin_count_from_package,
)
from backend.agents.symbol.tools.review import ReviewResult
from backend.agents.symbol.contracts.layout import SymbolLayout

# 少于这个数不算"解析出引脚表"。与 pipeline.MIN_TABLE_PINS 同源，这里不 import
# 是为了避免 selfcheck ↔ pipeline 的隐式耦合（pipeline 将来若要自检会成环）。
MIN_PINS = 2

# 引脚少于此数时，"全挤在一条边"可能只是小芯片的正常样子（SOT23-5 就 5 个脚）
ONE_SIDE_MIN_PINS = 8

Severity = Literal["error", "warn", "info"]

# 上传落盘用的任务号形态（`<job_id>.pdf`），防 Bug B 回归：
# 型号曾经被兜底成这个 hex 串，结果符号名成了 9b88e7418e27
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8,}$")

# 引脚名里出现汉字，多半是 OCR 残留（ADS1115 里 `-` 被读成「一」就是这一类）
_CJK_RE = re.compile(r"[一-鿿]")

_MAX_NAME_LEN = 24


@dataclass
class Question:
    """自检器提给用户的一个问题。调用方负责把它渲染成交互。

    `options` 为空表示自由输入；`default` 是无人应答时的兜底值 —— 超时不等于卡死。
    """

    key: str                                  # "package" —— 调用方靠它知道答案送回哪
    prompt: str
    options: list[str] = field(default_factory=list)
    default: str = ""


@dataclass
class Finding:
    """一条自检结论。`code` 是稳定标识，回归集和前端都靠它定位。"""

    code: str
    severity: Severity
    title: str                                # 一句话，结果页直接显示
    detail: str = ""                          # 具体证据：哪几个引脚、差多少
    hint: str = ""                            # 用户能做什么
    question: Question | None = None          # 非空 = 这条需要用户拍板

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "hint": self.hint,
            "question": (
                {
                    "key": self.question.key,
                    "prompt": self.question.prompt,
                    "options": list(self.question.options),
                    "default": self.question.default,
                }
                if self.question
                else None
            ),
        }


@dataclass
class CheckReport:
    findings: list[Finding] = field(default_factory=list)
    pin_count: int = 0

    # -------------------------------------------------------------- 查询
    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def questions(self) -> list[Finding]:
        """需要用户拍板的条目。调用方逐条去问，把答案套回对应参数重跑。"""
        return [f for f in self.findings if f.question is not None]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def verdict(self) -> str:
        if self.errors:
            return "broken"
        if self.warnings:
            return "suspect"
        return "ok"

    @property
    def score(self) -> int:
        """0–100，越高越好。

        权重取得很粗（error 25 / warn 5）—— 现在没人拿它做精细排序，将来 agent 循环
        要的是"这次比上次好"，只要单调、能解释得清就够了。
        """
        return max(0, 100 - 25 * len(self.errors) - 5 * len(self.warnings))

    def has(self, code: str) -> bool:
        return any(f.code == code for f in self.findings)

    # -------------------------------------------------------------- 输出
    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "ok": self.ok,
            "pin_count": self.pin_count,
            "counts": {
                "error": len(self.errors),
                "warn": len(self.warnings),
                "info": len(self.findings) - len(self.errors) - len(self.warnings),
            },
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_text(self) -> str:
        """给 CLI 打印 / 将来喂给 agent 的一段可读文本。"""
        head = f"自检：{self.verdict}（{self.score} 分，{self.pin_count} 个引脚）"
        if not self.findings:
            return head + "，没有发现问题"
        lines = [head]
        for finding in self.findings:
            tag = {"error": "错误", "warn": "警告", "info": "记录"}[finding.severity]
            lines.append(f"  [{tag}] {finding.title}")
            if finding.detail:
                lines.append(f"         {finding.detail}")
            if finding.hint:
                lines.append(f"         对策：{finding.hint}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 封装名 → 脚数
# 词汇表（封装式样表、`_plausible_pin_count`、`pin_count_from_package`）都搬去了
# `app/pkgname.py` —— 那边还要拿同一张表去正文里刮封装线索，留在这里会互相
# import 成环。顶部已转发导入，`from app.selfcheck import pin_count_from_package`
# 照旧可用（`tests/regress.py` 就是这么用的）。


# -------------------------------------------------------------------- 各条检查
def _is_number(text: str) -> bool:
    """纯数字。

    用 `str.isdigit()` 而不是正则 —— 它对汉字「一」返回 False（`isnumeric()` 才会
    返回 True），正好挡住 ADS1115 里那个被 OCR 读坏的 NC 行。
    """
    return bool(text) and text.strip().isdigit()


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_/]+", "", (text or "").strip().lower())


def _check_pins(pins: list[Pin], report: CheckReport) -> None:
    if not pins:
        report.findings.append(
            Finding(
                code="no_pins",
                severity="error",
                title="一个引脚都没解析出来",
                hint="多半是这份手册的引脚表排版没被认出来；换个封装名重跑，或确认表在第几页。",
            )
        )
        return

    if len(pins) < MIN_PINS:
        report.findings.append(
            Finding(
                code="too_few_pins",
                severity="error",
                title=f"只解析出 {len(pins)} 个引脚，不像一张完整的引脚表",
                hint="确认封装名填对了没有。",
            )
        )

    numbers = [pin.pin_number for pin in pins]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        report.findings.append(
            Finding(
                code="pin_numbers_duplicated",
                severity="error",
                title=f"有 {len(duplicates)} 个重复的引脚号",
                detail="、".join(duplicates[:20]) + ("…" if len(duplicates) > 20 else ""),
                hint="合并层不该产生重复；这是解析或合并的问题，请连同 PDF 一起反馈。",
            )
        )

    # 连续性：只在全是纯数字时查。BGA 那种 A1..K10 的编号不是数字，跳过
    if all(_is_number(n) for n in numbers):
        values = sorted(int(n) for n in numbers)
        expected = set(range(1, len(values) + 1))
        missing = sorted(expected - set(values))
        if missing:
            report.findings.append(
                Finding(
                    code="pin_numbers_not_contiguous",
                    severity="warn",
                    title=f"引脚号不是从 1 到 {len(values)} 的连续编号，缺 {len(missing)} 个",
                    detail="缺：" + "、".join(str(m) for m in missing[:20])
                    + ("…" if len(missing) > 20 else ""),
                    hint="可能是漏读了行，或表格被截断。核对一下引脚表。",
                )
            )


def _check_names(pins: list[Pin], report: CheckReport) -> None:
    if not pins:
        return

    empty = [pin.pin_number for pin in pins if not (pin.name or "").strip()]
    if empty:
        if len(empty) == len(pins):
            report.findings.append(
                Finding(
                    code="empty_pin_names",
                    severity="error",
                    title="所有引脚都没有名称",
                    hint="名称列没认出来。换个封装名重跑，或核对表格来源。",
                )
            )
        else:
            report.findings.append(
                Finding(
                    code="empty_pin_names",
                    severity="warn",
                    title=f"有 {len(empty)} 个引脚没有名称",
                    detail="、".join(empty[:20]) + ("…" if len(empty) > 20 else ""),
                    hint="对照引脚表看看这些位置本来该有没有名字。",
                )
            )

    if len(pins) >= 4:
        same = sum(1 for pin in pins if _norm(pin.name) == _norm(pin.pin_number))
        if same >= len(pins) * 0.6:
            report.findings.append(
                Finding(
                    code="name_looks_like_number",
                    severity="warn",
                    title=f"{same}/{len(pins)} 个引脚的名称和引脚号一模一样",
                    hint="多半是名称列没读到、错拿了引脚号列。换个封装名重跑看看。",
                )
            )

    dirty = [pin.pin_number for pin in pins if _CJK_RE.search(pin.name or "")]
    if dirty:
        report.findings.append(
            Finding(
                code="name_has_non_ascii",
                severity="warn",
                title=f"有 {len(dirty)} 个引脚的名称里混进了汉字",
                detail="、".join(dirty[:20]) + ("…" if len(dirty) > 20 else ""),
                hint="这是 OCR 残留（如把 `-` 读成「一」）。生成后请在结果页核对这些引脚名。",
            )
        )

    long_names = [pin.pin_number for pin in pins if len(pin.name or "") > _MAX_NAME_LEN]
    if long_names:
        report.findings.append(
            Finding(
                code="name_too_long",
                severity="info",
                title=f"有 {len(long_names)} 个引脚的名称超过 {_MAX_NAME_LEN} 个字符",
                detail="、".join(long_names[:10]) + ("…" if len(long_names) > 10 else ""),
                hint="可能把描述列当成名称列了，扫一眼引脚表。",
            )
        )


def _check_package(
    pins: list[Pin],
    package: str,
    candidates: list[str] | None,
    hints: list[PackageHint] | None,
    report: CheckReport,
) -> None:
    """封装名有没有定下来（这半边要问人），以及脚数和封装对不对得上（这半边只判对错）。

    **两半必须分开**，因为它们"能不能判"依赖不同的前提：

    - **候选/追问那半边**：`candidates is None` 表示这次压根没走"确认封装名"这条路
      （离线回归、批量分诊），整组跳过 —— 不然每个样本都会被"封装名没定下来"记一笔，
      工单里全是噪音。`[]` 和 `None` 是两回事：`[]` = 查过了但没有候选，照样要发问。
    - **脚数那半边**：只要手上有任何一个"这器件该多少脚"的来源就能判，**不依赖封装名
      有没有定下来**。独立出来之后，离线飞轮（零成本、没人可问）也能判脚数了 ——
      在拿去问人之前，先把"这对不上"这件事说出来。

    封装名是**用户确认过的输入**（CLI 会问），所以对不上不是"推断不准"，而是解析
    肯定错了 —— 因此是 error。

    脚数有两个来源，优先级：`pin_count_from_package(package)`（封装名自带）→
    `hinted_counts(hints)`（从手册正文刮到的"哪些封装、各多少脚"）。**两条路都没数
    就闭嘴** —— 宁可漏报，也不能拿猜的数去报警。

    两条路的底气不一样，文案上要留分寸：前者的封装名是**用户确认过的输入**，对不上
    就是解析错了；后者是从正文刮的线索，可能提到的是**同一家族的别的型号**（手册里
    顺手介绍兄弟料号时很常见）。所以提示语说的是"请确认封装名（或从候选里换一个）
    重跑"，而不是断言解析错了 —— 保留用户推翻它的余地。

    实测（2026-09-10 离线飞轮，17 份有缓存的芯片）：报出 3 份，逐条核对**都是真的**
    （7→16、5→20/28、30→32）；脚数对得上的那些（ADS1015 / ADS1115 / STM32 几份）
    一条没误报。剩下 8 份是压根没解析出引脚表（no_pins），走不到这一层。
    """
    candidates = None if candidates is None else [c for c in candidates if c]
    counts = hinted_counts(hints or [])

    # ---------------------------------------------------- 候选 / 追问那半边
    if candidates is not None:
        if not package:
            report.findings.append(
                Finding(
                    code="package_unknown",
                    severity="warn",
                    title="封装名没能确定",
                    hint="填上封装名重跑 —— 它决定从多封装并排的表里取哪一列。",
                    question=Question(
                        key="package",
                        prompt="这份手册没能确定封装名，请填一个（如 LQFP100 / DGS）",
                        options=candidates,
                        default=candidates[0] if candidates else "",
                    ),
                )
            )
        elif package not in candidates:
            report.findings.append(
                Finding(
                    code="package_ambiguous",
                    severity="info",
                    title=f"封装名「{package}」不在读到的候选里",
                    detail="读到的是：" + "、".join(candidates[:10]),
                    hint="确认一下用的是哪个封装；用错了会取错列。",
                    question=Question(
                        key="package",
                        prompt=f"封装名「{package}」不在候选里，要用哪个？",
                        options=candidates,
                        default=package,
                    ),
                )
            )

    # ------------------------------------------------------------ 脚数那半边
    if not pins:
        return

    expected = pin_count_from_package(package)
    if expected is not None:
        if expected == len(pins):
            return
        title = (
            f"引脚数和封装名对不上：{package} 应该有 {expected} 个，"
            f"实际解析出 {len(pins)} 个"
        )
        detail = f"差 {abs(expected - len(pins))} 个。"
        prompt = f"「{package}」应有 {expected} 个引脚，实际 {len(pins)} 个。封装名对吗？"
    elif counts:
        # 落在这份手册出现过的**任何一个**封装脚数上就算过 —— 多封装手册（STM32 一份
        # 覆盖 LQFP32/48/64、WLCSP36）给出的是一组值，不是单值。全对不上才是解析错了。
        if len(pins) in counts:
            return
        shown = " / ".join(str(count) for count in counts)
        title = f"引脚数和手册对不上：解析出 {len(pins)} 个，手册里的封装脚数是 {shown}"
        detail = describe_hints(hints or [])
        prompt = (
            f"手册里的封装是 {shown} 脚，实际解析出 {len(pins)} 个。"
            "是取错了列，还是表被截断了？"
        )
    else:
        return                                  # 两条路都没数 → 闭嘴

    # 候选名（人读出来的）和线索名（从正文刮的）都当选项 —— 后者常常就是正解
    options: list[str] = [package] if package else []
    for label in list(candidates or []) + package_candidates(hints or []):
        if label and label not in options:
            options.append(label)

    report.findings.append(
        Finding(
            code="pin_count_mismatch",
            severity="error",
            title=title,
            detail=detail,
            hint="对不上说明解析取错了列，或表被截断了。请确认封装名（或从候选里换一个）重跑。",
            question=Question(
                key="package",
                prompt=prompt,
                options=options,
                default=package or (options[0] if options else ""),
            ),
        )
    )


def _check_device(device: str, pdf_stem: str, report: CheckReport) -> None:
    if not device:
        report.findings.append(
            Finding(
                code="device_suspicious",
                severity="warn",
                title="型号为空，符号名可能没法用",
                hint="在型号框里填上型号重跑。",
            )
        )
        return

    if _JOB_ID_RE.match(device.lower()) and not device.isalpha():
        report.findings.append(
            Finding(
                code="device_suspicious",
                severity="warn",
                title=f"型号「{device}」看着像上传落盘的任务号，不像型号",
                hint="在型号框里手填型号重跑。",
            )
        )
    elif pdf_stem and device == pdf_stem:
        report.findings.append(
            Finding(
                code="device_suspicious",
                severity="warn",
                title=f"型号「{device}」是 PDF 文件名的原文，说明自动识别没认出型号",
                hint="在型号框里手填型号重跑，符号名会好看得多。",
            )
        )


def _check_sides(
    pins: list[Pin], layout: SymbolLayout | None, report: CheckReport
) -> None:
    # 这里看的是**布局前**的 pins。build_layout 会把 unknown 原地改写成 left，
    # 所以传布局后的那份进来，"全部未知"就变成了"全在左边"，虽然同样是 error，
    # 但话说不清楚了 —— service.py 用 deepcopy 喂布局正是为了保住这份信息。
    unknown = [pin.pin_number for pin in pins if pin.side == "unknown"]
    if unknown and len(unknown) == len(pins):
        report.findings.append(
            Finding(
                code="sides_unknown",
                severity="error",
                title=f"全部 {len(pins)} 个引脚的侧别都没定下来，符号等于废的",
                hint="引脚图没读到。确认封装名填对了没有 —— 多封装手册里读错图就会这样。",
            )
        )
    elif unknown:
        report.findings.append(
            Finding(
                code="sides_unknown",
                severity="warn",
                title=f"有 {len(unknown)} 个引脚侧别未定，已被默认放到左侧",
                detail="、".join(unknown[:20]) + ("…" if len(unknown) > 20 else ""),
                hint="这些引脚的位置需要人工摆一下。",
            )
        )

    if layout is None:
        return

    # 侧别全定下来了、却全挤在一条边 —— 引脚图只读到了半张，或只认出了第一条边
    known = [pin for pin in pins if pin.side != "unknown"]
    if len(known) < ONE_SIDE_MIN_PINS or len(known) != len(pins):
        return
    used = {pin.side for pin in known}
    if len(used) == 1:
        only = used.pop()
        report.findings.append(
            Finding(
                code="all_pins_one_side",
                severity="error",
                title=f"{len(known)} 个引脚全部落在{only}边，符号等于废的",
                hint="引脚图没读全，或者读的是另一张封装图。确认封装名后重跑。",
            )
        )


def _check_review(review: ReviewResult | None, pins: list[Pin], report: CheckReport) -> None:
    """复核差异：**按最终产物重新判一遍**，别拿中间态吓人。

    `review.mismatches` 是 ④ 的原始记录。在那之后，自动裁决（`AINO` → `AIN0` 这类
    字形易混）和用户的裁决都已经把值改过来了 —— 拿原始记录直接报警，会把一个
    早已解决的分歧说成"待核对"。所以这里拿差异去比对引脚现在的值，改对了就不再提。
    """
    if review is None:
        return

    current = {pin.pin_number: pin for pin in pins}

    def still_off(mismatch) -> bool:
        pin = current.get(mismatch.pin_number)
        if pin is None:
            return True                       # 复核说的引脚已经不在了，值得看
        if mismatch.field == "name":
            return _norm(pin.name) != _norm(mismatch.observed)
        if mismatch.field == "side":
            return pin.side != mismatch.observed
        return True

    names = [m for m in review.mismatches if m.field == "name" and still_off(m)]
    sides = [m for m in review.mismatches if m.field == "side" and still_off(m)]

    if review.missing:
        report.findings.append(
            Finding(
                code="review_missing",
                severity="warn",
                title=f"独立复核在图上没看到 {len(review.missing)} 个引脚",
                detail="、".join(review.missing[:20]) + ("…" if len(review.missing) > 20 else ""),
                hint="复核看的是引脚图。要么这些脚图上确实没有，要么表里多读了行。",
            )
        )
    if review.extra:
        report.findings.append(
            Finding(
                code="review_extra",
                severity="warn",
                title=f"独立复核在图上多出 {len(review.extra)} 个引脚",
                detail="、".join(review.extra[:20]) + ("…" if len(review.extra) > 20 else ""),
                hint="多半是表里漏了行。",
            )
        )

    if names:
        report.findings.append(
            Finding(
                code="review_name_mismatch",
                severity="warn",
                title=f"有 {len(names)} 处引脚名与独立复核对不上",
                detail="；".join(
                    f"{m.pin_number}: 提取 {m.expected} / 复核 {m.observed}"
                    for m in names[:10]
                ),
                hint="字形易混的那些已自动采信复核值；其余请在结果页核对。",
            )
        )
    if sides:
        report.findings.append(
            Finding(
                code="review_side_mismatch",
                severity="info",
                title=f"有 {len(sides)} 处引脚侧别与独立复核对不上",
                detail="；".join(
                    f"{m.pin_number}: 提取 {m.expected} / 复核 {m.observed}"
                    for m in sides[:10]
                ),
                hint="侧别差异一律不自动改：多封装手册里复核可能读的是另一张封装图。",
            )
        )


def _check_conflicts(conflicts: list[Conflict] | None, report: CheckReport) -> None:
    if not conflicts:
        return
    unresolved = [c for c in conflicts if c.resolved is None]
    if not unresolved:
        return
    report.findings.append(
        Finding(
            code="conflicts_unresolved",
            severity="warn",
            title=f"有 {len(unresolved)} 处双通道冲突没有裁决",
            detail="；".join(f"引脚 {c.pin_number} 的 {c.field}" for c in unresolved[:10]),
            hint="数据保持原样不动，请在结果页对照两边的值。",
        )
    )


def _check_capture(
    capture_passed: bool | None, files: Mapping[str, Path] | None, report: CheckReport
) -> None:
    """`capture_passed` 是三态：

    - `None` —— **还没生成**（CLI 的确认闸之前、离线回归），跳过整组检查
    - `False` —— 生成了但没出来（tclsh 找不到 / 超时 / TCL 报错）
    - `True` —— 生成成功

    把"没生成"和"生成失败"分开，是为了让同一份报告在确认闸之前也能用 ——
    那时候报"生成失败"是冤枉的。
    """
    if capture_passed is None:
        return

    if not capture_passed:
        report.findings.append(
            Finding(
                code="capture_failed",
                severity="error",
                title="Capture 没生成出 .olb / .dsn",
                hint="看生成日志里第一个 [FAIL]；找不到 tclsh 或 Capture 超时也会这样。",
            )
        )
        return

    if files is None:
        return
    absent = [kind for kind in ("olb", "dsn") if kind not in files]
    if absent:
        report.findings.append(
            Finding(
                code="artifact_missing",
                severity="warn",
                title=f"生成报告成功，但没找到这些产物：{'、'.join(absent)}",
                hint="到输出目录里确认一下。",
            )
        )


# -------------------------------------------------------------------- 入口
def check(
    pins: list[Pin],
    *,
    package: str = "",
    device: str = "",
    layout: SymbolLayout | None = None,
    review: ReviewResult | None = None,
    conflicts: list[Conflict] | None = None,
    table_warnings: list[str] | None = None,
    capture_passed: bool | None = None,
    files: Mapping[str, Path] | None = None,
    package_names: list[str] | None = None,
    package_hints: list[PackageHint] | None = None,
    sides_evaluated: bool = True,
    pdf_stem: str = "",
) -> CheckReport:
    """跑一遍全部检查。

    每个可选参数缺省时，对应的那一组检查就跳过 —— 这样同一个函数在"刚解析完还没
    生成"（离线回归、CLI 确认闸前）和"生成完了"（结果页）两种场合都能用。

    四个参数值得单独说：

    - `pins` 要传**布局之前**的那份。`build_layout` 会把 `side="unknown"` 原地改成
      `left`，传布局后的数据会让 `sides_unknown` 永远不触发。
    - `package_names=None` 表示没走"确认封装名"这条路，"封装名没定下来"那半边跳过；
      传 `[]` 则表示查过了但没候选，照样要发问。
    - `package_hints` 是从手册正文里刮到的封装线索（`app/pkgname.py`）。它让脚数判据
      **不依赖封装名有没有定下来** —— 所以离线分诊（不传 `package_names`）也能判脚数，
      抓到"ADC128S102 解出 7 个脚、手册写着 16-Lead TSSOP"这种真实误报。
      两边都不传，脚数判据就闭嘴。
    - `sides_evaluated=False` 表示视觉通道压根没跑（离线回归、批量分诊），
      这时侧别一律是 unknown，报出来是噪音不是结论。
    """
    report = CheckReport(pin_count=len(pins))
    _check_device(device, pdf_stem, report)
    _check_package(pins, package, package_names, package_hints, report)
    _check_pins(pins, report)
    _check_names(pins, report)
    if sides_evaluated:
        _check_sides(pins, layout, report)
    _check_review(review, pins, report)
    _check_conflicts(conflicts, report)
    _check_capture(capture_passed, files, report)

    if table_warnings:
        report.findings.append(
            Finding(
                code="upstream_warning",
                severity="info",
                title=f"解析过程有 {len(table_warnings)} 条降级记录",
                detail="；".join(table_warnings),
                hint="这些是程序自己降级处理的地方，列出来是为了留痕。",
            )
        )

    # 重的排前面，同级按检查顺序
    order = {"error": 0, "warn": 1, "info": 2}
    report.findings.sort(key=lambda f: order[f.severity])
    return report


def apply_answer(question: Question, value: str | None) -> str:
    """把用户对某个 question 的回答落成参数值。没答（None/空）就用默认值。"""
    answer = (value or "").strip()
    return answer or question.default


def ask_via(ask: Callable[[Question], str | None], finding: Finding) -> str:
    """把一个 finding 的 question 交给调用方的 ask 实现，返回可用的答案。"""
    assert finding.question is not None
    return apply_answer(finding.question, ask(finding.question))


def describe(report: CheckReport) -> str:
    return report.to_text()
