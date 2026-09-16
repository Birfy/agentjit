"""[design.md §4](../../docs/design.md#4-接口设计) 的四个操作。其余都是实现细节。

说到底就三件事：**一段话进去，长出代码，之后按名字拿回来。**

中间还有一步：`compile_function` 会**先让模型把测试用例写完整**，再让它写代码。
先后顺序是设计的一部分，理由见 `propose.py` 顶部 —— 一句话说就是，写测试的时候
代码还不存在，代码就没法反过来影响测试。

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

from .llm import LLMClient
from .lookup import Candidate, Lookup, find, search
from .propose import Proposal, propose_tests
from .registry import Function, NotCacheable, Registry
from .runtime import CallOutcome, Runtime, call_function  # noqa: F401  产品面的一员
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
    proposal: Proposal | None = None
    reason: str = ""
    review: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    @property
    def tokens(self) -> tuple[int, int]:
        """合成花掉的 token，含补用例那一次调用。"""
        i = o = 0
        if self.proposal:
            i, o = self.proposal.input_tokens, self.proposal.output_tokens
        if self.synth:
            i, o = i + self.synth.input_tokens, o + self.synth.output_tokens
        return i, o

    def render(self) -> str:
        lines = []
        if self.lookup:
            lines.append(self.lookup.render())
        if self.proposal:
            lines.append(self.proposal.render())
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
    gen_tests: int = 8,               # 先让模型补几条用例；0 = 只用调用方给的
    entry: str = "solve",
    **synth_kw: Any,
) -> CompileResult:
    """查一遍缓存，没有才合成。

    `cache`：
      - `auto`      先查后合成（默认）
      - `force_new` 跳过查找，强制合成一个新版本
      - `ephemeral` 合成一次就扔，不落盘。适合明知只用一次的需求

    `gen_tests`：先单独调一次模型，让它把用例补完整，再拿补完的用例去合成和验收。
    设成 0 就只用调用方给的那几条。代价是多一次调用；收益是判据变厚 ——
    但**不是变可信**，见 `propose.py`。
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

    # 先补用例，再写代码 —— 顺序是设计的一部分，见 propose.py
    prop = propose_tests(requirement, examples, client=client, n=gen_tests) \
        if gen_tests > 0 else None
    full = examples + (prop.examples if prop else [])

    r = synthesize(requirement, full, client=client, sandbox=sb,
                   thresholds=thresholds, entry=entry, **synth_kw)
    # 需求换了说法但本质是同一个函数时，新合成的会落在**本次**的 spec_hash 下。
    # 这不是 bug：两条说法各自留一份 hash，下次两边都能 L1 命中。
    state = "reused_with_new_version" if (lk and lk.stale) else "miss"
    if not r.ok:
        return CompileResult(status="failed", cache=state, spec=r.spec,
                             report=r.report, lookup=lk, synth=r, proposal=prop,
                             level=r.report.level if r.report else None,
                             reason=_blame(r, prop), review=r.review)

    if cache == "ephemeral":
        return CompileResult(status="ready", cache=state, spec=r.spec, synth=r,
                             lookup=lk, level=r.report.level, report=r.report,
                             proposal=prop,
                             reason="cache=ephemeral：没落盘，这个实现用完就没了。",
                             review=r.review)

    try:
        # 存的是**补完之后**的用例：生成的那几条也进测试集，标着 origin=generated，
        # 下次换模型重新生成时它们一起当验收标准。
        i, o = (prop.input_tokens if prop else 0), (prop.output_tokens if prop else 0)
        fn = reg.put(requirement, r.spec, r.code, r.report, full, model=model,
                     attempts=len(r.attempts), input_tokens=r.input_tokens + i,
                     output_tokens=r.output_tokens + o, name=name)
    except NotCacheable as e:
        return CompileResult(status="ready", cache=state, spec=r.spec, synth=r,
                             lookup=lk, level=r.report.level, report=r.report,
                             proposal=prop, reason=f"未入库：{e}", review=r.review)

    return CompileResult(status="ready", cache=state, spec=r.spec, synth=r, lookup=lk,
                         proposal=prop,
                         name=fn.name, handle=fn.handle, version=fn.versions[-1].name,
                         level=r.report.level, report=r.report, review=r.review)


def _blame(r: SynthResult, prop: Proposal | None = None) -> str:
    """失败该算谁的。

    只挂在**自动生成**的用例上时，结论是有歧义的：可能代码错了，也可能那条用例
    的期望值就是错的（模型算错一个边界是常事）。这种情况必须原样摆给调用方裁决，
    不能说成"代码有 bug" —— 拿一条错用例否决正确代码，比漏个 bug 难查得多。
    """
    g = r.report.gate("examples") if r.report else None
    fails = (g.detail.get("failures") or []) if g else []
    if not fails or any(f.get("origin", "caller") != "generated" for f in fails):
        return r.reason
    lines = ["代码通过了**你给的全部用例**，只挂在自动补的用例上 —— "
             "所以说不准是代码错了还是用例错了：", ""]
    assumed = {_key(e.input): e.assumes for e in (prop.examples if prop else [])
               if e.assumes}
    for f in fails[:4]:
        lines.append(f"  输入 {f['input']}")
        lines.append(f"  期望 {f['expected']}（自动生成）")
        lines.append(f"  实际 {f.get('error') or f.get('actual')}")
        if a := assumed.get(_key(f["input"])):
            # 这条挂掉最可能的原因：模型替需求做了个决定，而代码选了另一种读法。
            # 这时候"谁错了"根本不成立 —— 是需求没说清。
            lines.append(f"  ⚠ 这条压在一个需求没说的决定上：{a}")
        lines.append("")
    lines.append("请裁决：期望值对的话这就是代码的 bug；期望值错的话，"
                 "用 gen_tests=0 重编译，或者把正确的期望值补进 examples。")
    if any(assumed.get(_key(f["input"])) for f in fails):
        lines.append("上面带 ⚠ 的，多半不是谁错了，是**需求没说清** —— "
                     "把那个决定写进需求，再编译一次。")
    return "\n".join(lines)


def _key(value: Any) -> str:
    import json as _json
    return _json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _bank_the_hit(reg: Registry, lk: Lookup, examples: list[Example],
                  name: str = "") -> None:
    """命中之后把本次的用例并进去。

    它们刚刚通过了复验，所以是和当前实现一致的真判据 —— 一次缓存命中顺手把这个
    函数的用例变厚一点，下次换模型重新生成就更安全一点。
    """
    if name and lk.fn.name != name:
        lk.fn.name = name              # 命中了别人建的函数，顺手把名字贴上
        reg.save_spec(lk.fn)
    # 逐条加完再看有没有新的。any() 配生成器会短路，第一条加成功就不看后面的了。
    added = [lk.fn.tests.add(Example(ex.input, ex.output, note=ex.note,
                                     boundary=ex.boundary, origin="reverify"))
             for ex in examples]
    if any(added):
        reg.save_tests(lk.fn)


def get_code(name: str, *, registry: Registry | None = None) -> str:
    """按名字（或 handle）取代码。取不到返回空串。取的是最新的那个版本。"""
    fn = (registry or Registry()).get(name)
    v = fn.best() if fn else None
    return v.code if v else ""



def search_functions(query: str, *, registry: Registry | None = None,
                     limit: int = 10) -> list[Candidate]:
    """写需求之前先看看有没有现成的。只排序不复验 —— 这里回答的是
    "有没有像的"，不是"能不能用"，后者要例子。"""
    return search(registry or Registry(), query, limit)


def inspect_function(handle: str, *, registry: Registry | None = None) -> Function | None:
    return (registry or Registry()).get(handle)
