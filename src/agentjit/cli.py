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
    """从需求 + 例子合成一个函数，全程走完整验证。要花 token。"""
    from .llm import DEFAULT_MODEL, AnthropicClient
    from .synth import compile_function

    meta = _json.loads(Path(args.requirement).read_text())
    examples = [Example(**e) for e in meta["examples"]]
    client = AnthropicClient(model=args.model)

    print(f"需求: {meta['requirement'][:90]}")
    print(f"例子: {len(examples)} 个（边界 {sum(e.boundary for e in examples)} 个）　模型: {args.model}\n")

    r = compile_function(meta["requirement"], examples, client=client,
                         max_attempts=args.attempts)
    print(r.render())
    if r.ok and args.out:
        Path(args.out).write_text(r.code + "\n")
        print(f"\n已写入 {args.out}")
    elif r.ok and not args.out:
        print("\n" + r.code)
    return 0 if r.ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentjit", description="agent-jit 验证管线")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="验证一个用例目录")
    v.add_argument("case")
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("selftest", help="跑语料，确认每道关卡都抓得住它该抓的东西")
    s.add_argument("--corpus", default=None)
    s.set_defaults(fn=cmd_selftest)

    c = sub.add_parser("compile", help="从需求 + 例子合成一个函数（会调用 LLM）")
    c.add_argument("requirement", help="JSON 文件：{requirement, examples[]}")
    c.add_argument("--model", default=None)
    c.add_argument("--attempts", type=int, default=3)
    c.add_argument("-o", "--out", default=None, help="成功时把代码写到这里")
    c.set_defaults(fn=cmd_compile)

    args = p.parse_args(argv)
    if getattr(args, "model", "sentinel") is None:
        from .llm import DEFAULT_MODEL
        args.model = DEFAULT_MODEL
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
