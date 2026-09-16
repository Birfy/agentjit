"""合成循环：生成 → 静态检查 → 跑用例 → 结构化反馈 → 再生成，至多三次。

**反馈必须结构化。** "第 2 个例子期望 `{"sale": 300.0}` 实际 `{"sale": "300"}`"
能让模型一次修对；"没通过，再试试"只会让它随机重写。

早先这里还有保留集：把 30% 的用例藏起来不给修复循环看，防的是模型写出
`if input == X: return Y`。连同轮换机制一起删掉了 —— 正确性现在由调用方的
用例保证，模型能看见全部用例。**真要出问题多半出在这里**，`git log` 里能找回来。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

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
            lines.append(f"  尝试 {a.n}  {tag}")
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


def spec_for(requirement: str, examples: list[Example], entry: str = "solve") -> Spec:
    """从需求和例子推出规格。

    单独拆出来是因为查找要用：缓存的 key 里有 schema，所以得先有规格才能去查，
    而查中了就根本不用合成。
    """
    pschema, rschema = spec_schemas(examples)
    return Spec(intent=requirement.strip().splitlines()[0][:160],
                param_schema=pschema, return_schema=rschema, entry=entry)


def compile_function(
    requirement: str,
    examples: list[Example],
    *,
    client: LLMClient,
    sandbox: Sandbox | None = None,
    thresholds: Thresholds | None = None,
    max_attempts: int = 3,
    entry: str = "solve",
) -> SynthResult:
    th = thresholds or Thresholds()
    sb = sandbox or Sandbox()
    spec = spec_for(requirement, examples, entry)

    attempts: list[Attempt] = []
    code, report, why = _repair(requirement, spec, examples, client, sb, th,
                                max_attempts, attempts)
    if not code:
        last = attempts[-1].code if attempts else ""
        return SynthResult(False, spec, last, report, attempts, reason=why)
    return SynthResult(True, spec, code, report, attempts, review=review_flags(code))


def _repair(requirement, spec, examples, client, sb, th, max_attempts, attempts):
    """至多试 max_attempts 次，每次把上一次的具体失败喂回去。

    返回 (code, report, failure_reason)。通过的那一次的报告直接带出去 ——
    早先外面还要再验一遍（那时外面跑的是带保留集和变异测试的终审，和循环里
    那道不是同一回事）。现在两边一模一样，再验一遍纯属多跑一次沙箱。

    **失败时也带报告出去**：上面那层要看最后一次挂在哪几条用例上，才能判断
    该怪代码还是怪用例（jit.py 的 `_blame`）。只给一句 reason 的话，
    那个判断就做不了了。
    """
    feedback, last, last_rep = "", None, None
    for n in range(1, max_attempts + 1):
        user = build_user(requirement, examples, spec.param_schema, spec.return_schema, feedback)
        try:
            resp = client.complete(system=SYSTEM, user=user)
        except Refused as e:
            attempts.append(Attempt(n, "", "refused", str(e)))
            return "", None, f"模型拒答：{e}"

        code = extract_code(resp.text, spec.entry)
        if not code:
            attempts.append(Attempt(n, "", "no_code", "回复里没有代码块",
                                    resp.input_tokens, resp.output_tokens))
            feedback = render_feedback(resp.text[:600], "no_code",
                                       "回复里找不到 ```python 代码块", {})
            last = "no_code"
            continue

        rep = verify(code, spec, examples, thresholds=th, sandbox=sb)
        if not rep.failures:
            attempts.append(Attempt(n, code, "", "全部用例通过",
                                    resp.input_tokens, resp.output_tokens))
            return code, rep, ""

        g = rep.failures[0]
        attempts.append(Attempt(n, code, g.name, g.summary,
                                resp.input_tokens, resp.output_tokens))
        feedback = render_feedback(code, g.name, g.summary, g.detail)
        last, last_rep = g.name, rep

    return "", last_rep, (f"{max_attempts} 次尝试都没通过，最后卡在 {last}。"
                      "失败本身是有信息的 —— 多半说明这事不适合用代码做，"
                      "或者需求/例子之间本身不自洽。")
