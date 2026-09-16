"""验证管线的数据模型。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Level(str, Enum):
    """验证等级。见 docs/correctness.md §11。"""

    REJECTED = "REJECTED"        # 静态检查都没过，不该进沙箱
    EPHEMERAL = "EPHEMERAL"      # 能跑，但拿不出验收判据 —— 不进持久缓存
    VERIFIED = "VERIFIED"        # 全部关卡通过
    CONFIRMED = "CONFIRMED"      # VERIFIED + 调用方确认的变形性质（M1）


@dataclass
class Example:
    """一组输入/输出判据。"""

    input: dict[str, Any]
    output: Any
    note: str = ""
    boundary: bool = False       # 是否边界用例；VERIFIED 要求至少一个


@dataclass
class Spec:
    """一个待验证函数的规格。M0 只覆盖纯函数。"""

    intent: str
    param_schema: dict[str, Any]
    return_schema: dict[str, Any]
    entry: str = "solve"
    timeout_ms: int = 5000
    mem_mb: int = 512


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
