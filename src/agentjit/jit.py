"""[design.md §4](../../docs/design.md#4-接口设计) 的四个操作。其余都是实现细节。

说到底就三件事：**一段话进去，长出代码，之后按名字拿回来。**

    compile_function("按 score 排名次…", examples, name="rank")  → 入库
    get_code("rank")                                            → 源码
    call_function("rank", {"records": [...]})                   → 结果

外加两个辅助：`search_functions(query)` 找现成的，`inspect_function(name)` 看细节。

**生成方式是可替换的。** `compile_function` 只要一个满足 `LLMClient` 协议的对象
（`complete(system=, user=) -> LLMResponse`），换后端就是换这一个参数：

    AnthropicClient()    直连 API，要 ANTHROPIC_API_KEY
    ClaudeCliClient()    走本机 claude CLI，不要 key，用 Claude Code 的授权
    ScriptedClient([…])  回放预设答案，测试用，不打网络

这一层不干活，只负责把查找、合成、落盘按正确的顺序串起来。真正的逻辑在
`lookup.py`（查）、`synth.py`（合成）、`registry.py`（存）、`runtime.py`（调）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .econ import DEFAULT as DEFAULT_COST
from .econ import CostModel
from .llm import LLMClient
from .lookup import Candidate, Lookup, find, search
from .registry import Function, NotCacheable, Registry
from .runtime import CallOutcome, Runtime
from .sandbox import Sandbox
from .synth import SynthResult, compile_function as synthesize, spec_for
from .types import Example, Level, Report, Spec
from .verify import Thresholds


class NeedsClient(RuntimeError):
    """缓存没命中，但没给 LLM 客户端 —— 合成不了。"""


@dataclass
class CompileResult:
    status: str                       # ready | failed
    cache: str                        # hit | miss | reused_with_new_version
    spec: Spec
    handle: str = ""
    name: str = ""
    version: str = ""
    level: Level | None = None
    report: Report | None = None
    lookup: Lookup | None = None
    synth: SynthResult | None = None
    reason: str = ""
    review: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    @property
    def tokens(self) -> tuple[int, int]:
        return (self.synth.input_tokens, self.synth.output_tokens) if self.synth else (0, 0)

    def render(self) -> str:
        lines = []
        if self.lookup:
            lines.append(self.lookup.render())
        if self.synth:
            lines.append(self.synth.render())
        tag = {"hit": "命中缓存，没花 token",
               "miss": "没命中，合成了一个新的",
               "reused_with_new_version": "需求还是那句，但本次的例子和旧版本对不上 —— 新增了一个版本"}
        lines.append(f"\ncache: {self.cache}　{tag.get(self.cache, '')}")
        if self.handle:
            who = f"名字: {self.name}　handle: {self.handle}" if self.name else f"handle: {self.handle}"
            lines.append(f"{who}　{self.version}　"
                         f"等级 {self.level.value if self.level else '-'}")
        if self.reason:
            lines.append(self.reason)
        if self.review:
            lines.append("需人工过目: " + "；".join(self.review))
        return "\n".join(lines)


def compile_function(
    requirement: str,
    examples: list[Example],
    *,
    client: LLMClient | None = None,
    registry: Registry | None = None,
    sandbox: Sandbox | None = None,
    thresholds: Thresholds | None = None,
    cache: str = "auto",              # auto | force_new | ephemeral
    model: str = "",
    name: str = "",                   # 人起的名字，之后靠它取代码
    entry: str = "solve",
    **synth_kw: Any,
) -> CompileResult:
    """查一遍缓存，没有才合成。

    `cache`：
      - `auto`      先查后合成（默认）
      - `force_new` 跳过查找，强制合成一个新版本
      - `ephemeral` 合成一次就扔，不落盘。适合明知只用一次的需求
    """
    reg = registry or Registry()
    sb = sandbox or Sandbox()
    spec = spec_for(requirement, examples, entry)

    lk: Lookup | None = None
    if cache == "auto":
        lk = find(reg, requirement, spec, examples, sandbox=sb)
        if lk.hit:
            _bank_the_hit(reg, lk, examples, name)
            return CompileResult(status="ready", cache="hit", spec=lk.fn.spec,
                                 name=lk.fn.name,
                                 handle=lk.fn.handle, version=lk.version.name,
                                 level=lk.version.level, report=lk.version.report,
                                 lookup=lk)

    if client is None:
        raise NeedsClient(
            "缓存没命中，合成需要一个 LLM 客户端。"
            "只想查不想合成的话用 search_functions()。")

    r = synthesize(requirement, examples, client=client, sandbox=sb,
                   thresholds=thresholds, entry=entry, **synth_kw)
    # 需求换了说法但本质是同一个函数时，新合成的会落在**本次**的 spec_hash 下。
    # 这不是 bug：两条说法各自留一份 hash，下次两边都能 L1 命中。
    state = "reused_with_new_version" if (lk and lk.stale) else "miss"
    if not r.ok:
        return CompileResult(status="failed", cache=state, spec=r.spec,
                             report=r.report, lookup=lk, synth=r,
                             level=r.report.level if r.report else None,
                             reason=r.reason, review=r.review)

    if cache == "ephemeral":
        return CompileResult(status="ready", cache=state, spec=r.spec, synth=r,
                             lookup=lk, level=r.report.level, report=r.report,
                             reason="cache=ephemeral：没落盘，这个实现用完就没了。",
                             review=r.review)

    try:
        fn = reg.put(requirement, r.spec, r.code, r.report, examples, model=model,
                     attempts=len(r.attempts), input_tokens=r.input_tokens,
                     output_tokens=r.output_tokens, name=name)
    except NotCacheable as e:
        return CompileResult(status="ready", cache=state, spec=r.spec, synth=r,
                             lookup=lk, level=r.report.level, report=r.report,
                             reason=f"未入库：{e}", review=r.review)

    return CompileResult(status="ready", cache=state, spec=r.spec, synth=r, lookup=lk,
                         name=fn.name, handle=fn.handle, version=fn.versions[-1].name,
                         level=r.report.level, report=r.report, review=r.review)


def _bank_the_hit(reg: Registry, lk: Lookup, examples: list[Example],
                  name: str = "") -> None:
    """命中之后要记的两笔账。

    1. `reverify_passes += 1` —— 这个版本又一次用别人的标准验过了。
       `Function.best()` 的排序第二项就是它：被越多调用方验过的版本越可信。
    2. **本次的例子进测试集。** 它们刚刚通过了复验，所以是和当前实现一致的
       真判据。这是 correctness.md §10 里"测试集单调增长"最便宜的一条来源 ——
       一次缓存命中顺手把这个函数变厚了一点，下次换模型重生成就更安全一点。
    """
    v = lk.version
    v.stats.reverify_passes += 1
    if name and lk.fn.name != name:
        lk.fn.name = name              # 命中了别人建的函数，顺手把名字贴上
        reg.save_spec(lk.fn)
    for ex in examples:
        lk.fn.tests.add_example(Example(ex.input, ex.output, note=ex.note,
                                        boundary=ex.boundary, origin="reverify"))
    reg.save_tests(lk.fn)
    reg.save_version(lk.fn, v)


def get_code(name: str, *, registry: Registry | None = None) -> str:
    """按名字（或 handle）取代码。取不到返回空串。

    取的是 `best()` 那个版本 —— 被隔离的版本不会从这里出去。
    """
    fn = (registry or Registry()).get(name)
    v = fn.best() if fn else None
    return v.code if v else ""


def call_function(handle: str, args: dict[str, Any], *,
                  registry: Registry | None = None,
                  sandbox: Sandbox | None = None,
                  cost: CostModel = DEFAULT_COST) -> CallOutcome:
    rt = Runtime(registry, sandbox, cost)
    out = rt.call(handle, args)
    rt.flush()
    return out


def search_functions(query: str, *, registry: Registry | None = None,
                     limit: int = 10) -> list[Candidate]:
    """写需求之前先看看有没有现成的。只排序不复验 —— 这里回答的是
    "有没有像的"，不是"能不能用"，后者要例子。"""
    return search(registry or Registry(), query, limit)


def inspect_function(handle: str, *, registry: Registry | None = None) -> Function | None:
    return (registry or Registry()).get(handle)
