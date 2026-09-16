"""agent-jit —— 把文本需求编译成可复用的、经过验证的沙箱函数。

先让判官到位，再让选手上场，最后才谈复用：
验证管线（verify）→ 合成循环（synth）→ 落盘复用（registry / runtime）。
"""
from .types import Example, GateResult, Level, Report, Spec
from .econ import CostModel, Ledger
from .registry import Function, Registry, TestSet, Version
from .runtime import CallOutcome, Runtime, call_function
from .sandbox import Sandbox
from .verify import Thresholds, verify

__all__ = ["Example", "GateResult", "Level", "Report", "Spec",
           "Sandbox", "Thresholds", "verify",
           "CostModel", "Ledger", "Registry", "Function", "TestSet", "Version",
           "Runtime", "CallOutcome", "call_function"]
