"""agent-jit — text in, code out, fetch it back by name.

    compile_function(requirement, examples, name="rank")  -> synthesise, verify, store
    get_code("rank")                                      -> source
    call_function("rank", {...})                          -> run it in the sandbox

Correctness rests on **the test cases**. Beyond that there are only two gates: the
static check (generated code must not read files or import anything) and the sandbox
(it runs on your machine).
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
