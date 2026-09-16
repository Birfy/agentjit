"""调用一个已经编译好的函数。

第一性质（design.md §4.2）：

    call_function 永远不抛给调用方一个"看起来成功但其实错了"的结果。

所以每次调用是三步：**入参合不合 schema → 沙箱里跑 → 返回值合不合 schema**。
两道 schema 都是从例子结构反推的，不是猜的，而且和验证时用的是同一把尺子。

**调用不写盘。** 早先这里还要记调用次数、累计省了多少 token、连续失败几次就隔离
版本、把失败输入存回测试集 —— 那套东西需要真实流量才有意义，而在它有意义之前，
它让"调一次函数"变成了一个有状态、要 flush、要会话缓存的操作。现在调用是纯读，
没有 flush，没有缓存，进程之间也不会打架。

批量还留着：一次 `Sandbox.run` 能跑多个输入，进程只起一次。200 次调用逐个起进程
要 20 秒，批量 0.5 秒 —— 而"做 200 遍"正是这东西存在的理由。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jsonschema

from .registry import Function, Registry, Version, split_ref
from .sandbox import CallResult, RunResult, Sandbox

# 一个沙箱进程跑多少次调用。大了省进程启动，小了出事时重放的代价低。
BATCH = 50


@dataclass
class CallOutcome:
    ok: bool
    result: Any = None
    kind: str = ""            # guard_failed | runtime_error | budget_exceeded
    #                           | postcondition_failed
    message: str = ""
    version: str = ""

    def render(self) -> str:
        return f"ok    {self.version}" if self.ok else f"FAIL  {self.kind}: {self.message}"


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
    def __init__(self, registry: Registry | None = None, sandbox: Sandbox | None = None):
        self.reg = registry or Registry()
        self.sb = sandbox or Sandbox()

    def call(self, ref: str, args: dict[str, Any]) -> CallOutcome:
        return self.call_many(ref, [args])[0]

    def call_many(self, ref: str, args_list: list[dict[str, Any]]) -> list[CallOutcome]:
        fn = self.reg.get(ref)
        if fn is None:
            raise UnknownHandle(f"没有这个函数: {ref}")
        # `rank@v2` 指名要哪个版本；不指名就用最新的
        _, want = split_ref(ref)
        v = fn.version(want) if want else fn.best()
        if v is None:
            raise UnknownHandle(f"{fn.ref} 没有 {want or '任何'} 版本")

        out: list[CallOutcome] = []
        for i in range(0, len(args_list), BATCH):
            out.extend(self._batch(fn, v, args_list[i:i + BATCH]))
        return out

    # --- 内部 --------------------------------------------------------------
    def _batch(self, fn: Function, v: Version,
               args_list: list[dict[str, Any]]) -> list[CallOutcome]:
        results: list[CallOutcome | None] = [None] * len(args_list)

        # 1. 入参 guard。没过的根本不进沙箱 —— guard 必须比它保护的东西便宜。
        live: list[int] = []
        for i, args in enumerate(args_list):
            if msg := _schema_error(args, fn.spec.param_schema):
                results[i] = CallOutcome(False, kind="guard_failed", version=v.name,
                                         message=f"入参不合 param_schema —— {msg}")
            else:
                live.append(i)

        # 2. 沙箱
        if live:
            run = self.sb.run(v.code, fn.spec.entry, [args_list[i] for i in live],
                              timeout_ms=fn.spec.timeout_ms, mem_mb=fn.spec.mem_mb)
            if run.why_dead and len(live) > 1:
                # 整个进程死了，说不清是哪个输入干的 —— 逐个重放，把责任还给该负责的。
                # 不这么做的话，一颗老鼠屎会把同批 49 个无辜调用一起判死，
                # 而**错误的归因比错误本身更贵**。
                for i in live:
                    results[i] = self._batch(fn, v, [args_list[i]])[0]
            elif run.why_dead:
                results[live[0]] = self._dead(v, run)
            else:
                for slot, i in enumerate(live):
                    results[i] = self._judge(fn, v, run.results[slot])

        final = [r for r in results if r is not None]
        if len(final) != len(args_list):
            # 不该发生。真发生了也不能悄悄少还几个结果 —— 调用方多半在 zip，
            # 少一个就是从此每个结果都配错了输入。
            raise RuntimeError(f"批次少还了 {len(args_list) - len(final)} 个结果")
        return final

    def _judge(self, fn: Function, v: Version, r: CallResult) -> CallOutcome:
        if not r.ok:
            return CallOutcome(False, kind="runtime_error", message=r.error, version=v.name)
        if msg := _schema_error(r.value, fn.spec.return_schema):
            # 一个不合契约的结果交出去，就是"看起来成功但其实错了"。
            return CallOutcome(False, kind="postcondition_failed", version=v.name,
                               message=f"返回值不合 return_schema —— {msg}")
        return CallOutcome(True, result=r.value, version=v.name)

    def _dead(self, v: Version, run: RunResult) -> CallOutcome:
        kind = "budget_exceeded" if run.killed in ("timeout", "memory") else "runtime_error"
        return CallOutcome(False, kind=kind, message=run.why_dead, version=v.name)


def call_function(ref: str, args: dict[str, Any], *,
                  registry: Registry | None = None,
                  sandbox: Sandbox | None = None) -> CallOutcome:
    return Runtime(registry, sandbox).call(ref, args)
