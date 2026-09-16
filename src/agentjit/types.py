"""验证管线的数据模型。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Level(str, Enum):
    """验证结论。

    只有三档，因为只问三个问题：进得了沙箱吗、有判据吗、过了吗。
    （早先还有个 CONFIRMED，留给"调用方确认的变形性质"—— 那套机制删掉了，
    枚举值也就跟着删了，免得留一个任何代码路径都产不出来的档位。）
    """

    REJECTED = "REJECTED"        # 静态检查没过，或者用例没过
    EPHEMERAL = "EPHEMERAL"      # 能跑，但没有用例可判 —— 不进持久缓存
    VERIFIED = "VERIFIED"        # 通过了调用方给的全部用例


@dataclass
class Example:
    """一组输入/输出判据。"""

    input: dict[str, Any]
    output: Any
    note: str = ""
    boundary: bool = False       # 只是个标注，方便人看；不再影响判定
    # 这条判据是谁给的。用例只增不减，一年后回头看"这个期望值凭什么是它"，
    # 唯一能回答的就是出处。
    origin: str = "caller"       # caller | reverify | manual

    def to_dict(self) -> dict[str, Any]:
        return {"input": self.input, "output": self.output, "note": self.note,
                "boundary": self.boundary, "origin": self.origin}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Example":
        return cls(input=d["input"], output=d["output"], note=d.get("note", ""),
                   boundary=d.get("boundary", False), origin=d.get("origin", "caller"))


@dataclass
class Spec:
    """一个待验证函数的规格。M0 只覆盖纯函数。"""

    intent: str
    param_schema: dict[str, Any]
    return_schema: dict[str, Any]
    entry: str = "solve"
    timeout_ms: int = 5000
    mem_mb: int = 512

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "param_schema": self.param_schema,
                "return_schema": self.return_schema, "entry": self.entry,
                "timeout_ms": self.timeout_ms, "mem_mb": self.mem_mb}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Spec":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class GateResult:
    """一道关卡的结论。"""

    name: str
    passed: bool
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    blocking: bool = True        # False = 警告性关卡，不阻断（见 correctness.md §4.2）

    @property
    def icon(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL" if self.blocking else "WARN"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "summary": self.summary,
                "detail": self.detail, "blocking": self.blocking}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GateResult":
        return cls(name=d["name"], passed=d["passed"], summary=d["summary"],
                   detail=d.get("detail") or {}, blocking=d.get("blocking", True))


@dataclass
class Report:
    """一次完整验证的结论。"""

    level: Level
    gates: list[GateResult] = field(default_factory=list)
    wall_ms: float = 0.0

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed and g.blocking]

    def gate(self, name: str) -> GateResult | None:
        return next((g for g in self.gates if g.name == name), None)

    def render(self) -> str:
        w = max((len(g.name) for g in self.gates), default=0)
        lines = [f"{g.icon}  {g.name.ljust(w)}  {g.summary}" for g in self.gates]
        lines.append("")
        lines.append(f"级别: {self.level.value}   耗时: {self.wall_ms:.0f}ms")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level.value, "wall_ms": self.wall_ms,
                "gates": [g.to_dict() for g in self.gates]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Report":
        return cls(level=Level(d["level"]), wall_ms=d.get("wall_ms", 0.0),
                   gates=[GateResult.from_dict(g) for g in d.get("gates", [])])


def deep_equal(a: Any, b: Any, rel_tol: float = 1e-9, abs_tol: float = 1e-12) -> bool:
    """比较两个 JSON 值。数值给容差 —— 生成的代码用浮点算钱是常态，
    要求 bit 级相等会把正确的实现判错。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_equal(a[k], b[k], rel_tol, abs_tol) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_equal(x, y, rel_tol, abs_tol) for x, y in zip(a, b))
    return a == b
