"""合成循环。见 docs/design.md §6.3。

    生成 → 静态检查 → 跑用例 → 结构化反馈 → 再生成，至多三次。

两条纪律：

- **修复循环只能看见可见用例。** 保留集对它完全不可见，否则防过拟合就白做了
  （docs/correctness.md §9）。保留集挂了允许轮换一次重来 —— 轮换是防运气不好的
  分割，不是给模型再看一眼的机会。
- **不是所有失败都该反馈给模型。** 崩溃、不确定、死分支是代码问题，反馈回去能修；
  变异得分低是**测试集**问题，反馈回去只会让模型扭曲代码去迎合弱用例。后者要如实
  报给调用方："你的用例不够，缺的正是这几处"。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field, replace

from .infer import spec_schemas
from .llm import LLMClient, Refused
from .prompts import SYSTEM, build_user, render_feedback
from .sandbox import Sandbox
from .static_check import review_flags
from .types import Example, Report, Spec
from .verify import Thresholds, verify

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


@dataclass
class Attempt:
    n: int
    rotation: int
    code: str
    gate: str = ""          # 挂掉的关卡；"" = 通过
    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SynthResult:
    ok: bool
    spec: Spec
    code: str = ""
    report: Report | None = None
    attempts: list[Attempt] = field(default_factory=list)
    reason: str = ""
    review: list[str] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.attempts)

    def render(self) -> str:
        lines = []
        for a in self.attempts:
            tag = "通过" if not a.gate else f"{a.gate} —— {a.summary}"
            lines.append(f"  尝试 {a.rotation}.{a.n}  {tag}")
        if self.report:
            lines.append("")
            lines.append("  " + self.report.render().replace("\n", "\n  "))
        lines.append("")
        lines.append(f"结论: {'成功' if self.ok else '失败'}　"
                     f"token {self.input_tokens}in/{self.output_tokens}out")
        if self.reason:
            lines.append(self.reason)
        if self.review:
            lines.append("需人工过目: " + "；".join(self.review))
        return "\n".join(lines)


def extract_code(text: str, entry: str = "solve") -> str:
    """从回复里取出代码。优先取含入口函数的代码块。"""
    blocks = [b.strip() for b in _FENCE.findall(text)]
    for b in blocks:
        if f"def {entry}" in b:
            return b
    if blocks:
        return blocks[0]
    if f"def {entry}" in text:                     # 没加围栏但确实是代码
        try:
            ast.parse(text)
            return text.strip()
        except SyntaxError:
            pass
    return ""


def compile_function(
    requirement: str,
    examples: list[Example],
    *,
    client: LLMClient,
    sandbox: Sandbox | None = None,
    thresholds: Thresholds | None = None,
    max_attempts: int = 3,
    max_rotations: int = 1,
    entry: str = "solve",
) -> SynthResult:
    th = thresholds or Thresholds()
    sb = sandbox or Sandbox()

    # schema 从**全部**例子推，包括保留集 —— 结构是契约的一部分，不是答案。
    # 保留集要藏起来的是"这个输入对应哪个输出"，不是"输入长什么样"。
    pschema, rschema = spec_schemas(examples)
    spec = Spec(intent=requirement.strip().splitlines()[0][:160],
                param_schema=pschema, return_schema=rschema, entry=entry)

    attempts: list[Attempt] = []
    for rotation in range(max_rotations + 1):
        code, why = _repair(requirement, spec, examples, client, sb, th,
                            max_attempts, rotation, attempts)
        if not code:
            return SynthResult(False, spec, attempts=attempts, reason=why)

        report = verify(code, spec, examples, thresholds=th, sandbox=sb)
        if not report.failures:
            return SynthResult(True, spec, code, report, attempts,
                               review=review_flags(code))

        failed = report.failures[0]
        if failed.name == "examples.holdout" and rotation < max_rotations:
            continue          # 换一组保留集从头合成；模型看不到刚才为什么挂

        return SynthResult(False, spec, code, report, attempts,
                           reason=_explain(failed.name, report),
                           review=review_flags(code))

    return SynthResult(False, spec, attempts=attempts,
                       reason="轮换保留集后仍然没过 —— 多半在对可见用例过拟合")


def _repair(requirement, spec, examples, client, sb, th, max_attempts, rotation, attempts):
    """内层：只跟可见用例打交道。返回 (code, failure_reason)。"""
    from .holdout import NotEnoughExamples, split

    try:
        visible, held = (split(examples, th.holdout_ratio, th.holdout_seed, rotation)
                         if th.run_holdout else (examples, []))
    except NotEnoughExamples:
        visible, held = examples, []

    # 循环内用的判据：保留集和变异测试都关掉，其余关卡照跑 —— 崩溃、不确定、
    # 死分支都是模型能改的代码问题，早一轮告诉它，就少一轮浪费。
    loop_th = replace(th, run_holdout=False, run_mutation=False,
                      min_examples=1, require_boundary=False,
                      fuzz_n=max(40, th.fuzz_n // 4))

    feedback, last = "", None
    for n in range(1, max_attempts + 1):
        user = build_user(requirement, visible, spec.param_schema, spec.return_schema, feedback)
        try:
            resp = client.complete(system=SYSTEM, user=user)
        except Refused as e:
            attempts.append(Attempt(n, rotation, "", "refused", str(e)))
            return "", f"模型拒答：{e}"

        code = extract_code(resp.text, spec.entry)
        if not code:
            attempts.append(Attempt(n, rotation, "", "no_code", "回复里没有代码块",
                                    resp.input_tokens, resp.output_tokens))
            feedback = render_feedback(resp.text[:600], "no_code",
                                       "回复里找不到 ```python 代码块", {})
            last = "no_code"
            continue

        # 保留集的输入参与覆盖率统计，但不参与对错判定 —— 见 verify() 的说明
        rep = verify(code, spec, visible, thresholds=loop_th, sandbox=sb,
                     coverage_inputs=[e.input for e in held])
        if not rep.failures:
            attempts.append(Attempt(n, rotation, code, "", "可见用例全过",
                                    resp.input_tokens, resp.output_tokens))
            return code, ""

        g = rep.failures[0]
        attempts.append(Attempt(n, rotation, code, g.name, g.summary,
                                resp.input_tokens, resp.output_tokens))
        feedback = render_feedback(code, g.name, g.summary, g.detail)
        last = g.name

    return "", (f"{max_attempts} 次尝试都没通过，最后卡在 {last}。"
                "失败本身是有信息的 —— 多半说明这事不适合用代码做，"
                "或者需求/例子之间本身不自洽。")


def _explain(gate: str, report: Report) -> str:
    g = report.gate(gate)
    match gate:
        case "mutation":
            s = (g.detail.get("survivors", []) if g else [])
            return ("代码通过了全部用例，但**用例太弱**：下面这些改动没有任何用例能发现。\n"
                    + "\n".join(f"    {x}" for x in s)
                    + "\n这不是代码问题 —— 补用例覆盖这些地方，或者确认这些差异确实无所谓。")
        case "examples.holdout":
            return "轮换保留集后仍然没过 —— 多半在对可见用例过拟合。"
        case "coverage.branch":
            return f"分支覆盖没到 100%：{g.summary if g else ''}"
        case _:
            return f"终审卡在 {gate}：{g.summary if g else ''}"
