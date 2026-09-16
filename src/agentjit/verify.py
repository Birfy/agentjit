"""验证：静态检查 → 跑调用方给的用例。

**正确性由调用方的测试用例保证。** 所以这里只有两件事：一件是安全的
（生成的代码不该能读文件、不该能 import），一件是正确性的（它得通过你给的用例）。

早先这里还有五道关卡 —— 保留集、分支覆盖、模糊测试、确定性、变异测试。它们回答
的不是"这段代码对不对"，而是"你的用例够不够强"。那是**给调用方的建议，不是判定**，
而且每次要多花 570ms。全部删掉了，`git log` 里能找回来。

删掉的东西里有一条值得记着，将来真出问题多半是它：**保留集**（把 30% 的用例藏起来，
修复循环看不到，最后才跑）挡的是"模型对着可见用例写 `if input == X: return Y`"。
它不花额外 token。现在用例强度完全取决于调用方自觉。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jsonschema

from . import static_check
from .sandbox import RunResult, Sandbox
from .types import Example, GateResult, Level, Report, Spec, deep_equal


@dataclass
class Thresholds:
    # 一个用例都没有就没法判定对错 —— 那种情况只能 EPHEMERAL，不进持久缓存。
    min_examples: int = 1


def _schema_ok(value: Any, schema: dict) -> str:
    try:
        jsonschema.validate(value, schema)
        return ""
    except jsonschema.ValidationError as e:
        return f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
    except jsonschema.SchemaError as e:
        return f"schema 本身有问题: {e.message}"


def _failures(run: RunResult, examples: list[Example]) -> list[dict]:
    """不通过的用例。反馈必须结构化 —— 见 docs/design.md §6.3。
    "输入 X 期望 Y 实际 Z" 能让模型一次修对，"没通过，再试试"只会让它随机重写。"""
    bad = []
    for i, (ex, r) in enumerate(zip(examples, run.results)):
        if not r.ok:
            bad.append({"i": i, "input": ex.input, "expected": ex.output,
                        "actual": None, "error": r.error})
        elif not deep_equal(r.value, ex.output):
            bad.append({"i": i, "input": ex.input, "expected": ex.output, "actual": r.value})
    return bad


def verify(
    source: str,
    spec: Spec,
    examples: list[Example],
    *,
    thresholds: Thresholds | None = None,
    sandbox: Sandbox | None = None,
) -> Report:
    th = thresholds or Thresholds()
    sb = sandbox or Sandbox()
    gates: list[GateResult] = []
    t0 = time.perf_counter()

    def done(level: Level) -> Report:
        return Report(level=level, gates=gates, wall_ms=(time.perf_counter() - t0) * 1000)

    # ---- 静态检查：最便宜的一道，不过的根本不进沙箱 ------------------------
    violations = static_check.check(source, spec.entry)
    gates.append(GateResult("static", not violations,
                            "通过" if not violations else f"{len(violations)} 项违规: {violations[0]}",
                            {"violations": violations}))
    if violations:
        return done(Level.REJECTED)

    # ---- 有没有判据 --------------------------------------------------------
    enough = len(examples) >= th.min_examples
    gates.append(GateResult("examples.sufficiency", enough,
                            f"{len(examples)} 个用例" if enough
                            else "一个用例都没有 —— 判不了对错，只能 EPHEMERAL",
                            {"n": len(examples)}, blocking=False))
    if not enough:
        # 跑一次确认它至少不崩，但不进持久缓存：拿不出判据的实现存下来，
        # 就是把一个没人验过的东西摆上货架（design.md §8.1）。
        run = sb.run(source, spec.entry, [], timeout_ms=spec.timeout_ms, mem_mb=spec.mem_mb)
        gates.append(GateResult("examples", not run.why_dead, run.why_dead or "跳过：没有用例",
                                blocking=False))
        return done(Level.EPHEMERAL if not run.why_dead else Level.REJECTED)

    # ---- 跑用例：正确性就靠这一道 ------------------------------------------
    run = sb.run(source, spec.entry, [e.input for e in examples],
                 timeout_ms=spec.timeout_ms, mem_mb=spec.mem_mb)
    if why := run.why_dead:
        gates.append(GateResult("examples", False, why,
                                {"load_error": run.load_error, "killed": run.killed}))
        return done(Level.REJECTED)

    bad = _failures(run, examples)
    gates.append(GateResult("examples", not bad,
                            f"{len(examples) - len(bad)}/{len(examples)} 通过",
                            {"failures": bad}))
    if bad:
        return done(Level.REJECTED)

    # ---- 返回值合不合契约 --------------------------------------------------
    # schema 是从例子结构反推的，不是猜的。这道几乎不花时间，顺手做掉 ——
    # 调用时还会再查一遍（runtime 的返回 guard），两边用的是同一把尺子。
    off = [{"i": i, "value": r.value, "why": msg}
           for i, r in enumerate(run.results)
           if r.ok and (msg := _schema_ok(r.value, spec.return_schema))]
    gates.append(GateResult("return_schema", not off,
                            "通过" if not off else f"{len(off)} 个返回值不合 return_schema",
                            {"violations": off[:5]}))
    return done(Level.REJECTED if off else Level.VERIFIED)
