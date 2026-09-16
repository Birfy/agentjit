from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import json as _json

from .cases import Case, load_all
from .types import Example
from .verify import Thresholds, verify

CORPUS = Path(__file__).resolve().parents[2] / "tests" / "corpus"


def _print_details(report, verbose: bool) -> None:
    for g in report.gates:
        if g.passed or not g.detail:
            continue
        if not verbose and g.name == "mutation":
            for s in g.detail.get("survivors", []):
                print(f"      存活: {s}")
            continue
        print(f"      {g.name}:")
        print("      " + json.dumps(g.detail, ensure_ascii=False, indent=2,
                                    default=str).replace("\n", "\n      ")[:1800])


def cmd_verify(args) -> int:
    case = Case.load(Path(args.case))
    report = verify(case.source, case.spec, case.examples)
    print(f"== {case.name} ==")
    print(report.render())
    _print_details(report, args.verbose)
    return 0 if not report.failures else 1


def cmd_selftest(args) -> int:
    """M0 的出口判据：验证关卡能抓住人为植入的错误实现。

    每个语料用例都针对一道关卡。关卡没抓住，或者抓错了地方，都算失败 ——
    一个在正确代码上误报的关卡，比一个漏报的关卡更难排查。
    """
    cases = load_all(Path(args.corpus or CORPUS))
    if not cases:
        print("语料为空", file=sys.stderr)
        return 1

    th = Thresholds()
    width = max(len(c.name) for c in cases)
    bad = 0
    for case in cases:
        report = verify(case.source, case.spec, case.examples, thresholds=th)
        got_failing = sorted(g.name for g in report.failures)
        want_failing = sorted(case.expect_failing)
        ok = report.level.value == case.expect_level and got_failing == want_failing

        mark = "ok  " if ok else "MISS"
        print(f"{mark} {case.name.ljust(width)}  {report.level.value:<10} "
              f"{'+'.join(got_failing) or '-':<40} {report.wall_ms:6.0f}ms   {case.why}")
        if not ok:
            bad += 1
            print(f"      预期 {case.expect_level} / {'+'.join(want_failing) or '-'}")
            print("      " + report.render().replace("\n", "\n      "))
            _print_details(report, args.verbose)

    print(f"\n{len(cases) - bad}/{len(cases)} 个语料用例符合预期")
    return 1 if bad else 0


def cmd_compile(args) -> int:
    """先查缓存，没有才合成。只有合成那条路花 token。"""
    from .jit import compile_function
    from .registry import Registry

    meta = _json.loads(Path(args.requirement).read_text())
    examples = [Example.from_dict(e) for e in meta["examples"]]

    print(f"需求: {meta['requirement'][:90]}")
    print(f"例子: {len(examples)} 个（边界 {sum(e.boundary for e in examples)} 个）　"
          f"模型: {args.model}　cache: {args.cache}\n")

    try:
        r = compile_function(meta["requirement"], examples, client=_LazyClient(args.model),
                             registry=Registry(args.home), cache=args.cache,
                             model=args.model, max_attempts=args.attempts)
    except NoCredentials as e:
        print(e, file=sys.stderr)
        return 2
    print(r.render())
    if not r.ok:
        return 1
    code = r.synth.code if r.synth else _code_of(args.home, r.handle)
    if args.out and code:
        Path(args.out).write_text(code + "\n")
        print(f"\n已写入 {args.out}")
    elif code:
        print("\n" + code)
    return 0


class NoCredentials(RuntimeError):
    pass


class _LazyClient:
    """真要合成的时候才构造客户端。

    这样没凭据的机器上 `agentjit compile` 仍然能查缓存 —— 命中就根本不需要
    凭据。构造 `AnthropicClient` 会 import anthropic 并读凭据，放在最前面做，
    等于让"有没有现成的"这个问题也依赖网络。
    """

    def __init__(self, model: str):
        self.model, self._real = model, None

    def complete(self, **kw):
        if self._real is None:
            try:
                from .llm import AnthropicClient
                self._real = AnthropicClient(model=self.model)
            except Exception as e:
                # 没装包、没登录、凭据过期都走这里。这是最常撞上的一条路，
                # 不该甩一串 traceback 出去 —— 要说清楚下一步做什么。
                raise NoCredentials(
                    f"缓存没命中，合成需要 Anthropic 客户端，但起不来：{e}\n"
                    "    pip install anthropic\n"
                    "    ant auth login          # 别让浏览器授权那步超时\n"
                    "只想用现成的函数就跑 agentjit search / agentjit list。") from e
        return self._real.complete(**kw)


def _code_of(home, handle: str) -> str:
    from .registry import Registry
    fn = Registry(home).get(handle)
    v = fn.best() if fn else None
    return v.code if v else ""


def cmd_search(args) -> int:
    """写需求之前先看看有没有现成的。只排序不复验 —— 复验要例子。"""
    from .jit import search_functions
    from .registry import Registry

    hits = search_functions(args.query, registry=Registry(args.home), limit=args.limit)
    if not hits:
        print("registry 是空的")
        return 0
    for c in hits:
        print(f"{c.similarity:.2f}  {c.fn.handle}@{c.version.name}  "
              f"{c.version.level.value:<10}{c.fn.requirement[:56]}")
    print("\n相似度只用来缩小候选集。「求和」和「求平均」在字面上非常近 —— "
          "真正的判定要拿你的例子跑一遍复验（compile 会做）。")
    return 0


def cmd_adopt(args) -> int:
    """把一份**人写的**实现验一遍再入库，不花 token。

    它不是合成的替代品，是两件别的事：把回归语料装进 registry 好跑 call/bench，
    以及给"模型升级 = 免费的全库重生成"留一个不依赖模型的对照实现。
    入库的 `synth_input_tokens` 是 0 —— 它确实没花，net_savings 也就不该拿它说事。
    """
    from .registry import NotCacheable, Registry

    case = Case.load(Path(args.case))
    report = verify(case.source, case.spec, case.examples)
    print(f"== {case.name} ==")
    print(report.render())
    if report.failures:
        _print_details(report, args.verbose)
        return 1
    try:
        fn = Registry(args.home).put(case.spec.intent, case.spec, case.source,
                                     report, case.examples, model="human")
    except NotCacheable as e:
        print(f"\n未入库：{e}")
        return 1
    print(f"\nhandle: {fn.handle}　（{fn.versions[-1].name}，合成成本 0 —— 人写的）")
    return 0


def cmd_list(args) -> int:
    from .registry import Registry
    from .runtime import Runtime

    rt = Runtime(Registry(args.home))
    fns = rt.reg.all()
    if not fns:
        print(f"registry 是空的（{rt.reg.root}）")
        return 0

    print(f"{'handle':<18}{'等级':<12}{'版本':<7}{'调用':>7}{'用例':>6}{'探针':>6}"
          f"{'net_savings':>14}   需求")
    for fn in fns:
        v = fn.best()
        led = rt.ledger(fn)
        level = v.level.value if v else "QUARANTINED"
        print(f"{fn.handle:<18}{level:<12}{(v.name if v else '-'):<7}"
              f"{led.calls:>7}{len(fn.tests.examples):>6}{len(fn.tests.probes):>6}"
              f"{led.net_savings:>14,.0f}   {fn.requirement[:44]}")
    return 0


def cmd_inspect(args) -> int:
    from .registry import Registry
    from .runtime import Runtime

    rt = Runtime(Registry(args.home))
    fn = rt.reg.get(args.handle)
    if fn is None:
        print(f"没有这个函数: {args.handle}", file=sys.stderr)
        return 1

    print(f"== {fn.handle} ==")
    print(f"需求: {fn.requirement}")
    print(f"入口: {fn.spec.entry}(params, ctx)　超时 {fn.spec.timeout_ms}ms　"
          f"内存 {fn.spec.mem_mb}MB　建于 {fn.created_at}")

    print(f"\n-- 测试集（{len(fn.tests.examples)} 例 + {len(fn.tests.probes)} 探针）--")
    for e in fn.tests.examples:
        tag = "边界" if e.boundary else "　　"
        print(f"  {tag} [{e.origin}] {_json.dumps(e.input, ensure_ascii=False)[:60]}"
              f" → {_json.dumps(e.output, ensure_ascii=False)[:40]}")
    for p in fn.tests.probes:
        print(f"  探针 [{p.kind}×{p.seen}] {_json.dumps(p.input, ensure_ascii=False)[:60]}"
              f"　{p.detail[:60]}")

    print("\n-- 版本 --")
    best = fn.best()
    for v in fn.versions:
        s = v.stats
        mark = "★" if best and v.name == best.name else " "
        print(f" {mark}{v.name}  {v.level.value:<10}{v.state:<12}"
              f"调用 {s.calls}（ok {s.ok} / guard {s.guard_failed} / "
              f"错 {s.attributable} / 警告 {s.warnings}）　"
              f"合成 {v.synth_input_tokens}in/{v.synth_output_tokens}out　{v.model}")
        # 复验通过次数是 best() 排序的第二项：被越多调用方用自己的例子验过越可信。
        # 不打出来的话，为什么选了这个版本就成了黑箱。
        print(f"      复验通过 {s.reverify_passes} 次"
              + (f"　线上失败率 {s.failure_rate:.1%}" if s.attributable else ""))
        if v.properties:
            print(f"      后置断言（只警告）: {', '.join(v.properties)}")
        if v.quarantine_reason:
            print(f"      隔离原因: {v.quarantine_reason}")

    print("\n-- 账 --")
    print(rt.ledger(fn).render(rt.cost))

    if args.code and best:
        print(f"\n-- {best.name} 代码 --")
        print(best.code)
    return 0


def cmd_call(args) -> int:
    from .registry import Registry
    from .runtime import Runtime, UnknownHandle

    rt = Runtime(Registry(args.home))
    payload = Path(args.args).read_text() if Path(args.args).exists() else args.args
    try:
        out = rt.call(args.handle, _json.loads(payload))
    except UnknownHandle as e:
        print(e, file=sys.stderr)
        return 1
    rt.flush()
    print(out.render())
    if out.ok:
        print(_json.dumps(out.result, ensure_ascii=False, indent=2))
    return 0 if out.ok else 1


def cmd_bench(args) -> int:
    """完成判据那条线：compile 一次，call N 次，看 net_savings 什么时候转正。

    输入用模糊器从 param_schema + 测试集例子生成 —— 不是重放例子。重放例子只能
    证明缓存能读回来，生成输入才会真的把 guard 链路压出来。
    """
    from .registry import Registry
    from .runtime import Runtime, UnknownHandle
    from . import fuzz

    rt = Runtime(Registry(args.home))
    fn = rt.reg.get(args.handle)
    if fn is None:
        print(f"没有这个函数: {args.handle}", file=sys.stderr)
        return 1

    before = rt.ledger(fn)
    inputs = fuzz.make_inputs(fn.spec.param_schema,
                              [e.input for e in fn.tests.examples], args.calls, args.seed)
    try:
        outs = rt.call_many(fn.handle, inputs)
    except UnknownHandle as e:
        print(e, file=sys.stderr)
        return 1
    rt.flush()

    kinds: dict[str, int] = {}
    for o in outs:
        kinds[o.kind or "ok"] = kinds.get(o.kind or "ok", 0) + 1
    warned = sum(1 for o in outs if o.warnings)

    fn = rt.reg.get(args.handle)           # 重新读，隔离/统计都已落盘
    after = rt.ledger(fn)
    print(f"== bench {fn.handle} × {args.calls} ==")
    print("　".join(f"{k} {v}" for k, v in sorted(kinds.items()))
          + (f"　后置断言警告 {warned}" if warned else ""))
    print()
    print(after.render(rt.cost))
    if before.calls:
        print(f"（本次之前已调用 {before.calls} 次，上面是累计数）")
    if after.synth_cost == 0:
        print("注意：这个函数的合成成本是 0（人写的或没记录），"
              "net_savings 转正不说明任何问题 —— 要看真实合成的数。")
    return 0


def cmd_stats(args) -> int:
    from .econ import Ledger
    from .registry import Registry
    from .runtime import Runtime

    rt = Runtime(Registry(args.home))
    fns = rt.reg.all()
    total = Ledger()
    quarantined = 0
    for fn in fns:
        led = rt.ledger(fn)
        total.saved += led.saved
        total.synth_cost += led.synth_cost
        total.calls += led.calls
        total.ok += led.ok
        quarantined += sum(1 for v in fn.versions if not v.active)

    print(f"函数 {len(fns)} 个　版本 {sum(len(f.versions) for f in fns)} 个"
          f"（隔离 {quarantined}）　测试集共 "
          f"{sum(len(f.tests.examples) for f in fns)} 例 + "
          f"{sum(len(f.tests.probes) for f in fns)} 探针")
    print()
    print(total.render(rt.cost))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentjit", description="agent-jit")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--home", default=None,
                   help="registry 根目录，默认 $AGENTJIT_HOME/registry 或 ~/.agentjit/registry")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="验证一个用例目录")
    v.add_argument("case")
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("selftest", help="跑语料，确认每道关卡都抓得住它该抓的东西")
    s.add_argument("--corpus", default=None)
    s.set_defaults(fn=cmd_selftest)

    c = sub.add_parser("compile", help="先查缓存，没有才合成（合成会调用 LLM）")
    c.add_argument("requirement", help="JSON 文件：{requirement, examples[]}")
    c.add_argument("--model", default=None)
    c.add_argument("--attempts", type=int, default=3)
    c.add_argument("-o", "--out", default=None, help="成功时把代码写到这里")
    c.add_argument("--cache", choices=["auto", "force_new", "ephemeral"], default="auto",
                   help="auto=先查后合成；force_new=强制合成新版本；ephemeral=合成一次不落盘")
    c.set_defaults(fn=cmd_compile)

    se = sub.add_parser("search", help="写需求前先看看有没有现成的")
    se.add_argument("query")
    se.add_argument("-n", "--limit", type=int, default=10)
    se.set_defaults(fn=cmd_search)

    a = sub.add_parser("adopt", help="把一份人写的实现验一遍再入库，不花 token")
    a.add_argument("case", help="语料目录，含 case.json")
    a.set_defaults(fn=cmd_adopt)

    ls = sub.add_parser("list", help="registry 里有什么")
    ls.set_defaults(fn=cmd_list)

    i = sub.add_parser("inspect", help="一个函数的测试集、版本和账")
    i.add_argument("handle")
    i.add_argument("--code", action="store_true", help="连最优版本的源码一起打印")
    i.set_defaults(fn=cmd_inspect)

    cl = sub.add_parser("call", help="调用一个已编译的函数")
    cl.add_argument("handle")
    cl.add_argument("args", help="JSON 字面量，或一个 JSON 文件路径")
    cl.set_defaults(fn=cmd_call)

    b = sub.add_parser("bench", help="调 N 次，看 net_savings 什么时候转正")
    b.add_argument("handle")
    b.add_argument("-n", "--calls", type=int, default=200)
    b.add_argument("--seed", type=int, default=7)
    b.set_defaults(fn=cmd_bench)

    st = sub.add_parser("stats", help="全库的收支")
    st.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    if getattr(args, "model", "sentinel") is None:
        from .llm import DEFAULT_MODEL
        args.model = DEFAULT_MODEL
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
