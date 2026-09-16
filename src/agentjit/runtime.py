"""`call_function` —— 调用一个已经编译好的函数。

见 [design.md §4.2](../../docs/design.md#42-call_function) 和
[§8.2 Guard](../../docs/design.md#82-guard)。这条链路的第一性质是：

    call_function 永远不抛给调用方一个"看起来成功但其实错了"的结果。

所以每一次调用都是：

    入参 schema guard → 沙箱执行 → 返回 schema → 后置断言 → 记账

三个和文档字面不同的地方，都是想清楚之后故意的：

1. **入参 guard 失败不计入隔离。** 文档说"连续 3 次 guard 失败 → QUARANTINED"。
   但入参没过 schema 的输入**根本没进函数**，它不构成任何对这个实现的指控。
   而 param_schema 是从两三个例子推出来的，天生偏窄 —— 让它把一个正确的函数
   隔离掉，是拿调用方的错误惩罚实现。所以入参 guard 只记账、只进测试集
   （schema 太窄本身就是要修的东西），不推进隔离计数。
   能算到实现头上的是：运行时崩溃、超预算、返回值不合 schema。

2. **后置断言只警告不阻断**，见 assertions.py 顶部。

3. **批量执行。** 一次 `Sandbox.run` 可以跑多个输入，进程只起一次。200 次调用
   逐个起进程要 20 秒，批量 1 秒。批量里有输入把进程整个搞死时（超时/内存），
   退回逐个执行重放一遍 —— 否则一颗老鼠屎会把同批次 49 个无辜的调用一起判死，
   而**错误的归因比错误本身更贵**。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from . import assertions
from .econ import DEFAULT as DEFAULT_COST
from .econ import CostModel, Ledger
from .registry import Function, Probe, Registry, Version, parse_handle
from .sandbox import CallResult, RunResult, Sandbox

# 连续多少次"算得到实现头上"的失败就隔离该版本。design.md §6.5。
QUARANTINE_AFTER = 3

# 一个沙箱进程跑多少次调用。大了省进程启动，小了出事时重放的代价低。
BATCH = 50


@dataclass
class CallOutcome:
    ok: bool
    result: Any = None
    kind: str = ""            # guard_failed | runtime_error | budget_exceeded
    #                           | postcondition_failed | quarantined | unknown_handle
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    version: str = ""
    saved: float = 0.0
    wall_ms: float = 0.0

    def render(self) -> str:
        head = (f"ok    {self.version}" if self.ok
                else f"FAIL  {self.kind}: {self.message}")
        if self.warnings:
            head += "\n      警告（不阻断）: " + "；".join(self.warnings)
        return head


class UnknownHandle(KeyError):
    pass


def _schema_error(value: Any, schema: dict) -> str:
    try:
        jsonschema.validate(value, schema)
        return ""
    except jsonschema.ValidationError as e:
        return f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
    except jsonschema.SchemaError as e:
        return f"schema 本身有问题: {e.message}"


class Runtime:
    """一次会话。统计在内存里累加，`flush()` 才落盘 ——
    200 次调用写 200 次 meta.json 是纯浪费。"""

    def __init__(self, registry: Registry | None = None, sandbox: Sandbox | None = None,
                 cost: CostModel = DEFAULT_COST):
        self.reg = registry or Registry()
        self.sb = sandbox or Sandbox()
        self.cost = cost
        self._cache: dict[str, Function] = {}
        self._alias: dict[str, str] = {}
        self._dirty: set[str] = set()

    # --- 对外 --------------------------------------------------------------
    def function(self, handle: str) -> Function:
        """会话内只读一次盘。

        必须缓存：连续失败计数是跨调用累加的，每次都从盘上重读，等于每次都把
        计数清零，隔离永远触发不了。（这条是被测试逼出来的 —— 第一版就没缓存。）
        代价是会话期间看不到别的进程对 registry 的改动，M0 单进程，认了。
        """
        key = self._alias.get(handle)
        if key is None:
            fn = self.reg.get(handle)
            if fn is None:
                raise UnknownHandle(f"没有这个函数: {handle}")
            key = self._alias[handle] = fn.spec_hash
            self._cache.setdefault(key, fn)
        return self._cache[key]

    def call(self, handle: str, args: dict[str, Any]) -> CallOutcome:
        return self.call_many(handle, [args])[0]

    def call_many(self, handle: str, args_list: list[dict[str, Any]]) -> list[CallOutcome]:
        fn = self.function(handle)
        # `fn_7a3c9e@v2` 指名要哪个版本；不指名就按 best() 排序取最优。
        # 指名一个被隔离的版本不给过 —— 隔离的意思就是别再用它了，
        # 写得出版本号也不构成例外。
        _, want = parse_handle(handle)
        v = fn.version(want) if want else fn.best()
        if v is None or not v.active:
            why = (v.quarantine_reason if v is not None else
                   next((x.quarantine_reason for x in fn.versions if x.quarantine_reason), ""))
            what = f"{fn.handle}@{want}" if want else f"{fn.handle} 的所有版本"
            return [CallOutcome(False, kind="quarantined",
                                message=f"{what}不可用。{why}")
                    for _ in args_list]

        out: list[CallOutcome] = []
        for i in range(0, len(args_list), BATCH):
            out.extend(self._batch(fn, v, args_list[i:i + BATCH]))
        v.stats.last_used = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        self._dirty.add(fn.spec_hash)
        return out

    def flush(self) -> None:
        for h in self._dirty:
            fn = self._cache[h]
            self.reg.save_tests(fn)
            for v in fn.versions:
                self.reg.save_version(fn, v)
        self._dirty.clear()

    def ledger(self, fn: Function) -> Ledger:
        """一个函数的收支。合成成本按**所有**版本累加 —— 重新合成过的函数
        回本要更久，这笔账不该因为换了版本就一笔勾销。"""
        led = Ledger()
        for v in fn.versions:
            led.saved += v.stats.saved
            led.calls += v.stats.calls
            led.ok += v.stats.ok
            led.synth_cost += self.cost.synth(v.synth_input_tokens, v.synth_output_tokens)
        return led

    # --- 内部 --------------------------------------------------------------
    def _batch(self, fn: Function, v: Version,
               args_list: list[dict[str, Any]]) -> list[CallOutcome]:
        t0 = time.perf_counter()
        results: list[CallOutcome | None] = [None] * len(args_list)

        # 1. 入参 guard。没过的根本不进沙箱 —— guard 必须比它保护的东西便宜。
        live: list[int] = []
        for i, args in enumerate(args_list):
            if msg := _schema_error(args, fn.spec.param_schema):
                results[i] = self._guard_failed(fn, v, args, msg)
            else:
                live.append(i)

        # 2. 沙箱执行
        if live:
            run = self.sb.run(v.code, fn.spec.entry, [args_list[i] for i in live],
                              timeout_ms=fn.spec.timeout_ms, mem_mb=fn.spec.mem_mb)
            if run.why_dead and len(live) > 1:
                # 整个进程死了，说不清是哪个输入干的 —— 逐个重放，把责任还给该负责的
                for i in live:
                    results[i] = self._batch(fn, v, [args_list[i]])[0]
            elif run.why_dead:
                results[live[0]] = self._dead(fn, v, args_list[live[0]], run)
            else:
                for slot, i in enumerate(live):
                    results[i] = self._judge(fn, v, args_list[i], run.results[slot])

        wall = (time.perf_counter() - t0) * 1000
        final = [r for r in results if r is not None]
        if len(final) != len(args_list):
            # 不该发生。真发生了也不能悄悄少还几个结果 —— 调用方多半在 zip，
            # 少一个就是从此每个结果都配错了输入。
            raise RuntimeError(f"批次少还了 {len(args_list) - len(final)} 个结果")
        share = wall / max(1, len(final))
        for r in final:
            r.version, r.wall_ms = r.version or v.name, r.wall_ms or share
        return final

    # --- 判定 --------------------------------------------------------------
    def _judge(self, fn: Function, v: Version, args: dict,
               r: CallResult) -> CallOutcome:
        if not r.ok:
            return self._failure(fn, v, args, "runtime_error", r.error,
                                 probe_detail=r.error)

        if msg := _schema_error(r.value, fn.spec.return_schema):
            # 返回 schema 不是猜的：它从例子结构读出来、在 200 个模糊输入上验过。
            # 所以它阻断 —— 一个不合契约的结果交出去，就是"看起来成功但其实错了"。
            return self._failure(fn, v, args, "postcondition_failed",
                                 f"返回值不合 return_schema —— {msg}", probe_detail=msg)

        broken = assertions.check(v.properties, args, r.value)
        v.stats.calls += 1
        v.stats.ok += 1
        v.stats.consecutive_failures = 0
        saved = self.cost.saving(fn.requirement, args, r.value)
        v.stats.saved += saved

        if broken:
            # 违反的是没人确认过的性质 —— 不拦结果，但输入要留下来。
            # 它要么是个真 bug，要么说明这条性质一开始就挖错了，两种都得有人看。
            v.stats.warnings += 1
            fn.tests.add_probe(Probe(input=args, kind="postcondition",
                                     detail="；".join(broken), version=v.name))
        return CallOutcome(True, result=r.value, warnings=broken,
                           version=v.name, saved=saved)

    def _dead(self, fn: Function, v: Version, args: dict, run: RunResult) -> CallOutcome:
        kind = "budget_exceeded" if run.killed in ("timeout", "memory") else "runtime_error"
        return self._failure(fn, v, args, kind, run.why_dead, probe_detail=run.why_dead)

    def _guard_failed(self, fn: Function, v: Version, args: dict, msg: str) -> CallOutcome:
        """入参没过 schema。**不推进隔离计数** —— 见模块顶部第 1 条。

        但输入要存：param_schema 是从几个例子推出来的，被拒的输入十有八九说明
        schema 推窄了，而不是调用方错了。这是重新合成时最该看的一类证据。
        """
        v.stats.calls += 1
        v.stats.guard_failed += 1
        fn.tests.add_probe(Probe(input=args, kind="guard", detail=msg, version=v.name))
        return CallOutcome(False, kind="guard_failed",
                           message=f"入参不合 param_schema —— {msg}", version=v.name)

    def _failure(self, fn: Function, v: Version, args: dict, kind: str, msg: str,
                 *, probe_detail: str) -> CallOutcome:
        v.stats.calls += 1
        setattr(v.stats, kind, getattr(v.stats, kind) + 1)   # kind 与 Stats 的字段同名
        v.stats.consecutive_failures += 1
        fn.tests.add_probe(Probe(input=args, kind=kind, detail=probe_detail[:400],
                                 version=v.name))

        if v.stats.consecutive_failures >= QUARANTINE_AFTER and v.active:
            self.reg.quarantine(
                fn, v, f"连续 {v.stats.consecutive_failures} 次失败，最后一次是 {kind}: {msg[:120]}")
        return CallOutcome(False, kind=kind, message=msg, version=v.name)


def call_function(handle: str, args: dict[str, Any], *,
                  registry: Registry | None = None,
                  sandbox: Sandbox | None = None,
                  cost: CostModel = DEFAULT_COST) -> CallOutcome:
    """单次调用的便利入口。每次都落盘；连着调很多次请直接用 `Runtime`。"""
    rt = Runtime(registry, sandbox, cost)
    out = rt.call(handle, args)
    rt.flush()
    return out
