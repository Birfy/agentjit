"""验证管线的编排。见 docs/correctness.md。

关卡顺序按"便宜的先跑"排：静态检查不花时间，挡掉的东西根本不用进沙箱。

M0 只实现不需要 LLM 的关卡：T1（例子）、T5（模糊/确定性），以及量化测试集强度的
分支覆盖和变异测试。T2（变形性质）、T3（差分）、T4（影子）是 M1。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from . import fuzz, mutate, static_check
from .holdout import NotEnoughExamples, split
from .sandbox import RunResult, Sandbox
from .types import Example, GateResult, Level, Report, Spec, deep_equal


@dataclass
class Thresholds:
    min_examples: int = 2
    require_boundary: bool = True
    holdout_ratio: float = 0.3
    holdout_seed: int = 0
    fuzz_n: int = 200
    fuzz_seed: int = 0
    determinism_n: int = 30
    mutation_limit: int = 60
    min_mutation_score: float = 0.80
    min_branch_coverage: float = 1.0
    min_line_coverage: float = 1.0
    max_survivors_shown: int = 8
    # 第一道阻断关卡挂掉就停。后面的关卡在一份已知是错的代码上跑不出有用信息，
    # 而合成的修复循环本来也只看第一个失败。
    fail_fast: bool = True
    # 修复循环里把这两道关掉：保留集必须对循环不可见（否则防过拟合就白做了），
    # 变异得分衡量的是"测试集强不强"，不是代码问题，反馈给模型只会让它扭曲代码去迎合弱用例。
    run_holdout: bool = True
    run_mutation: bool = True


@dataclass
class _Ctx:
    source: str
    spec: Spec
    examples: list[Example]
    th: Thresholds
    sb: Sandbox
    gates: list[GateResult] = field(default_factory=list)

    def add(self, name, passed, summary, detail=None, blocking=True) -> GateResult:
        g = GateResult(name, passed, summary, detail or {}, blocking)
        self.gates.append(g)
        return g


def _schema_ok(value: Any, schema: dict) -> str:
    try:
        jsonschema.validate(value, schema)
        return ""
    except jsonschema.ValidationError as e:
        return f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
    except jsonschema.SchemaError as e:
        return f"schema 本身有问题: {e.message}"


def _check_examples(run: RunResult, examples: list[Example], offset: int) -> list[dict]:
    """返回不通过的用例清单。反馈必须结构化 —— 见 docs/design.md §6.3。"""
    bad = []
    for i, ex in enumerate(examples):
        r = run.results[offset + i]
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
    coverage_inputs: list[dict[str, Any]] | None = None,
) -> Report:
    """coverage_inputs：只参与覆盖率统计、不参与对错判定的额外输入。

    修复循环需要它。循环只能看见可见用例，但"这段代码有没有被验证过"问的是
    **整个测试集**——正确实现的某个分支很可能只有保留集那条用例能走到。拿可见
    用例量覆盖率，会逼着模型删掉必要的代码，删了再被保留集判死。
    覆盖率只需要输入，不需要期望输出，所以这里用全部输入量、只用可见用例判对错，
    反馈里也只说"第几行没覆盖"—— 保留集的输入和答案都不会泄漏给模型。
    """
    th = thresholds or Thresholds()
    c = _Ctx(source, spec, examples, th, sandbox or Sandbox())
    t0 = time.perf_counter()

    # ---- 静态检查 ----------------------------------------------------------
    violations = static_check.check(source, spec.entry)
    c.add("static", not violations,
          "通过" if not violations else f"{len(violations)} 项违规: {violations[0]}",
          {"violations": violations})
    if violations:
        return _finish(c, Level.REJECTED, t0)

    # ---- 判据是否足够 ------------------------------------------------------
    n = len(examples)
    has_boundary = any(e.boundary for e in examples)
    suff_msgs = []
    if n < th.min_examples:
        suff_msgs.append(f"用例 {n} < {th.min_examples}")
    if th.require_boundary and not has_boundary:
        suff_msgs.append("缺少边界用例")
    sufficient = not suff_msgs
    c.add("examples.sufficiency", sufficient,
          f"{n} 个用例，边界 {'有' if has_boundary else '无'}" if sufficient
          else "；".join(suff_msgs) + " —— 只能 EPHEMERAL",
          {"n": n, "has_boundary": has_boundary}, blocking=False)

    try:
        visible, held = (split(examples, th.holdout_ratio, th.holdout_seed)
                         if sufficient and th.run_holdout else (examples, []))
    except NotEnoughExamples:
        visible, held = examples, []

    # ---- 跑例子（顺带量覆盖率）--------------------------------------------
    ordered = visible + held
    run = c.sb.run(source, spec.entry,
                   [e.input for e in ordered] + list(coverage_inputs or []),
                   coverage=True, timeout_ms=spec.timeout_ms, mem_mb=spec.mem_mb)

    if why := run.why_dead:
        c.add("examples.visible", False, why, {"load_error": run.load_error, "killed": run.killed})
        return _finish(c, Level.REJECTED, t0)

    bad_v = _check_examples(run, visible, 0)
    c.add("examples.visible", not bad_v,
          f"{len(visible) - len(bad_v)}/{len(visible)} 通过" if bad_v else f"{len(visible)}/{len(visible)} 通过",
          {"failures": bad_v})

    # 可见用例没过的时候，保留集的结论没有信息量：它要回答的是"可见用例过了但
    # 保留集没过吗"，前提不成立就不该记一笔。不止损的话，一份对所有输入都崩溃的
    # 代码会同时挂掉两道关，归因变成平台相关的
    #（Linux 的 RLIMIT_AS 让子进程逐次抛 MemoryError，macOS 那边整个进程被看门狗杀掉，
    # 后者走 why_dead 提前返回，只留下一道关卡）。
    if (bail := _bail(c, t0)):
        return bail

    if held:
        bad_h = _check_examples(run, held, len(visible))
        c.add("examples.holdout", not bad_h,
              f"{len(held) - len(bad_h)}/{len(held)} 通过"
              + ("　← 可见用例过了但保留集没过，典型的过拟合" if bad_h else ""),
              {"failures": bad_h})
    else:
        c.add("examples.holdout", True,
              "跳过：修复循环内不分保留集" if not th.run_holdout else "跳过：用例不足，分不出保留集",
              blocking=False)

    if (bail := _bail(c, t0)):
        return bail

    # ---- 模糊 + 确定性（T5）-----------------------------------------------
    inputs = fuzz.make_inputs(spec.param_schema, [e.input for e in examples],
                              th.fuzz_n, th.fuzz_seed)
    valid = [x for x in inputs if not _schema_ok(x, spec.param_schema)]
    invalid_n = len(inputs) - len(valid)

    frun = c.sb.run(source, spec.entry, valid, timeout_ms=spec.timeout_ms, mem_mb=spec.mem_mb)
    if why := frun.why_dead:
        c.add("fuzz.crash", False, f"{len(valid)} 个 schema 合法输入 —— {why}",
              {"killed": frun.killed})
        return _finish(c, Level.REJECTED, t0)

    crashes = [{"input": valid[i], "error": r.error}
               for i, r in enumerate(frun.results) if not r.ok]
    c.add("fuzz.crash", not crashes,
          f"{len(valid)} 个 schema 合法输入，{len(crashes)} 个崩溃"
          + (f"（另有 {invalid_n} 个非法输入未计入）" if invalid_n else ""),
          {"crashes": crashes[:5], "n_valid": len(valid)})

    schema_bad = []
    for i, r in enumerate(frun.results):
        if r.ok and (msg := _schema_ok(r.value, spec.return_schema)):
            schema_bad.append({"input": valid[i], "value": r.value, "why": msg})
    c.add("fuzz.schema", not schema_bad,
          f"{len(schema_bad)} 个返回值不符合 return_schema",
          {"violations": schema_bad[:5]})

    det_inputs = valid[: th.determinism_n]
    drun = c.sb.run(source, spec.entry, det_inputs, timeout_ms=spec.timeout_ms, mem_mb=spec.mem_mb)
    # 两次都是独立子进程，hash 种子天然不同 —— 依赖集合序或当前时间的代码会在这里露馅
    nondet = [{"input": det_inputs[i]}
              for i in range(len(det_inputs))
              if not (frun.results[i].ok == drun.results[i].ok
                      and deep_equal(frun.results[i].value, drun.results[i].value))]
    c.add("determinism", not nondet,
          f"{len(det_inputs)} 个输入跑两遍，{len(nondet)} 个结果不一致"
          + ("　← 用了集合序/当前时间/随机数？" if nondet else ""),
          {"cases": nondet[:3]})

    if (bail := _bail(c, t0)):
        return bail

    # 判据不足时，覆盖率和变异得分都量不出有意义的东西 —— 它们衡量的是"测试集强不强"，
    # 而这里根本没有测试集。跳过，定级为 EPHEMERAL：能跑一次，但不进持久缓存。
    if not sufficient:
        c.add("coverage.branch", True, "跳过：判据不足", blocking=False)
        c.add("mutation", True, "跳过：判据不足", blocking=False)
        return _finish(c, Level.EPHEMERAL, t0)

    # ---- 分支覆盖 ----------------------------------------------------------
    cov = run.coverage or {}
    if "summary" not in cov:
        c.add("coverage.branch", False, f"覆盖率采集失败: {cov.get('error', '无数据')}")
    else:
        s = cov["summary"]
        nb, cb = s.get("num_branches", 0), s.get("covered_branches", 0)
        nl, cl = s.get("num_statements", 0), s.get("covered_lines", 0)
        br = cb / nb if nb else 1.0
        ln = cl / nl if nl else 1.0
        ok = br >= th.min_branch_coverage and ln >= th.min_line_coverage
        c.add("coverage.branch", ok,
              f"分支 {cb}/{nb}　行 {cl}/{nl}"
              + ("" if ok else f"　← 未覆盖的分支就是未验证的代码：行 {cov.get('missing_lines')}"),
              {"missing_lines": cov.get("missing_lines"),
               "missing_branches": cov.get("missing_branches"),
               "branch_rate": br, "line_rate": ln})

    if (bail := _bail(c, t0)):
        return bail

    # ---- 变异测试 ----------------------------------------------------------
    if not th.run_mutation:
        c.add("mutation", True, "跳过：修复循环内不跑变异测试", blocking=False)
        return _finish(c, Level.VERIFIED if sufficient else Level.EPHEMERAL, t0)

    mutants = mutate.generate(source, th.mutation_limit)
    if not mutants:
        c.add("mutation", False, "生成不出有效变异体（代码太简单？）", blocking=False)
    else:
        calls = [e.input for e in ordered]
        runs = c.sb.run_many([
            {"source": m.source, "entry": spec.entry, "calls": calls,
             "timeout_ms": spec.timeout_ms, "mem_mb": spec.mem_mb}
            for m in mutants
        ])
        survivors = [m for m, mr in zip(mutants, runs) if not _killed(mr, ordered)]
        score = 1 - len(survivors) / len(mutants)
        ok = score >= th.min_mutation_score
        c.add("mutation", ok,
              f"杀死 {len(mutants) - len(survivors)}/{len(mutants)}　得分 {score:.0%}"
              + ("" if ok else "　← 测试集有盲区，下面这些改动没人发现"),
              {"score": score,
               "survivors": [f"{m.kind}　{m.where}" for m in survivors[: th.max_survivors_shown]],
               "total": len(mutants)})

    # ---- 定级 --------------------------------------------------------------
    if any(not g.passed and g.blocking for g in c.gates):
        return _finish(c, Level.REJECTED, t0)
    return _finish(c, Level.VERIFIED, t0)


def _bail(c: _Ctx, t0: float) -> Report | None:
    if c.th.fail_fast and any(not g.passed and g.blocking for g in c.gates):
        return _finish(c, Level.REJECTED, t0)
    return None


def _killed(mr: RunResult, examples: list[Example]) -> bool:
    """变异体被杀死 = 测试集发现了这个改动。崩溃、超时、结果不符都算。"""
    if mr.timed_out or mr.load_error or not mr.ok:
        return True
    if len(mr.results) != len(examples):
        return True
    return bool(_check_examples(mr, examples, 0))


def _finish(c: _Ctx, level: Level, t0: float) -> Report:
    return Report(level=level, gates=c.gates, wall_ms=(time.perf_counter() - t0) * 1000)
