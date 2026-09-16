"""agent-jit —— 一段话进去，长出代码，之后按名字拿回来。

    compile_function(需求, examples, name="rank")  → 合成 + 验证 + 入库
    get_code("rank")                              → 源码
    call_function("rank", {...})                  → 在沙箱里跑一次

正确性由**调用方给的用例**保证。除此之外只有两道关：静态检查（生成的代码不该能
读文件、不该能 import）和沙箱（它在你的机器上执行）。
"""
from .types import Example, GateResult, Level, Report, Spec
from .registry import Function, Registry, TestSet, Version
from .runtime import CallOutcome, Runtime
from .sandbox import Sandbox
from .verify import Thresholds, verify
from .jit import (CompileResult, call_function, compile_function, get_code,
                  inspect_function, search_functions)

__all__ = ["Example", "GateResult", "Level", "Report", "Spec",
           "Sandbox", "Thresholds", "verify",
           "Registry", "Function", "TestSet", "Version",
           "Runtime", "CallOutcome", "CompileResult",
           "compile_function", "get_code", "call_function",
           "search_functions", "inspect_function"]
