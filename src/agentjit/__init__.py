"""agent-jit —— 把文本需求编译成可复用的、经过验证的沙箱函数。

M0 只有验证管线：先让判官到位，再让选手上场。
"""
from .types import Example, GateResult, Level, Report, Spec
from .sandbox import Sandbox
from .verify import Thresholds, verify

__all__ = ["Example", "GateResult", "Level", "Report", "Spec",
           "Sandbox", "Thresholds", "verify"]
