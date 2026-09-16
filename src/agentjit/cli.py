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
          f"模型: {args.model}　后端: {args.via}　cache: {args.cache}\n")

    try:
        r = compile_function(meta["requirement"], examples,
                             client=_generator(args.via, args.model),
                             registry=Registry(args.home), cache=args.cache,
                             model=args.model, name=args.name or meta.get("name", ""),
                             max_attempts=args.attempts)
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


def _generator(via: str, model: str):
    """挑一个生成后端。合成方式是可替换的，换的就是这一个对象。"""
    if via == "cli":
        from .llm import ClaudeCliClient
        return ClaudeCliClient(model=model)
    return _LazyClient(model)


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
        print(f"{c.similarity:.2f}  {c.fn.ref}@{c.version.name}  "
              f"{c.version.level.value:<10}{c.fn.requirement[:56]}")
    print("\n相似度只用来缩小候选集。「求和」和「求平均」在字面上非常近 —— "
          "真正的判定要拿你的例子跑一遍复验（compile 会做）。")
    return 0



def cmd_list(args) -> int:
    from .registry import Registry

    reg = Registry(args.home)
    fns = reg.all()
    if not fns:
        print(f"registry 是空的（{reg.root}）")
        return 0

    print(f"{'名字/handle':<20}{'版本':<7}{'用例':>5}   需求")
    for fn in fns:
        v = fn.best()
        print(f"{fn.ref:<20}{(v.name if v else '-'):<7}{len(fn.tests.examples):>5}"
              f"   {fn.requirement[:58]}")
    return 0


def cmd_inspect(args) -> int:
    from .registry import Registry

    reg = Registry(args.home)
    fn = reg.get(args.handle)
    if fn is None:
        print(f"没有这个函数: {args.handle}", file=sys.stderr)
        return 1

    print(f"== {fn.ref} ==" + (f"　（{fn.handle}）" if fn.name else ""))
    print(f"需求: {fn.requirement}")
    print(f"入口: {fn.spec.entry}(params, ctx)　超时 {fn.spec.timeout_ms}ms　"
          f"内存 {fn.spec.mem_mb}MB　建于 {fn.created_at}")

    print(f"\n-- 用例（{len(fn.tests.examples)} 个）--")
    for e in fn.tests.examples:
        print(f"  [{e.origin}] {_json.dumps(e.input, ensure_ascii=False)[:64]}"
              f" → {_json.dumps(e.output, ensure_ascii=False)[:44]}")
        if e.assumes:
            print(f"      ⚠ 压在一个需求没说的决定上：{e.assumes}")

    print("\n-- 版本 --")
    best = fn.best()
    for v in fn.versions:
        mark = "★" if best and v.name == best.name else " "
        print(f" {mark}{v.name}  {v.level.value:<10}{v.created_at}　{v.model or '-'}"
              f"　尝试 {v.attempts} 次　token {v.input_tokens}in/{v.output_tokens}out")


    if args.code and best:
        print(f"\n-- {best.name} 代码 --")
        print(best.code)
    return 0


def cmd_get(args) -> int:
    """按名字（或 handle）把代码打出来。这就是"查询对应名字的代码"。"""
    from .jit import get_code
    from .registry import Registry

    code = get_code(args.name, registry=Registry(args.home))
    if not code:
        print(f"没有叫 {args.name!r} 的函数（试试 agentjit list）", file=sys.stderr)
        return 1
    print(code, end="" if code.endswith("\n") else "\n")
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
    print(out.render())
    if out.ok:
        print(_json.dumps(out.result, ensure_ascii=False, indent=2))
    return 0 if out.ok else 1




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
    c.add_argument("--name", default=None, help="给它起个名字，之后 agentjit get <名字>")
    c.add_argument("--via", choices=["api", "cli"], default="cli",
                   help="生成后端：cli=走本机 claude（不要 API key）；api=直连 Anthropic")
    c.set_defaults(fn=cmd_compile)

    g = sub.add_parser("get", help="按名字把代码打出来")
    g.add_argument("name")
    g.set_defaults(fn=cmd_get)

    se = sub.add_parser("search", help="写需求前先看看有没有现成的")
    se.add_argument("query")
    se.add_argument("-n", "--limit", type=int, default=10)
    se.set_defaults(fn=cmd_search)

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

    args = p.parse_args(argv)
    if getattr(args, "model", "sentinel") is None:
        from .llm import DEFAULT_MODEL
        args.model = DEFAULT_MODEL
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
