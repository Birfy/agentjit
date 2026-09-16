"""The synthesis loop: write, static-check, run the cases, feed the failure back,
write again — at most three times.

**The feedback has to be structured.** "case 2 expected `{"sale": 300.0}`, got
`{"sale": "300"}`" lets the model fix it in one shot; "didn't pass, try again" just
makes it rewrite at random.

There used to be a hold-out split here: 30% of the cases hidden from the repair loop,
to stop the model writing `if input == X: return Y`. It was removed along with the
rotation logic — correctness now rests on the caller's cases, and the model sees all
of them. **If something goes wrong, this is the most likely place**; `git log` has it.
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
    gate: str = ""          # the gate that failed; "" means it passed
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
            tag = "passed" if not a.gate else f"{a.gate} — {a.summary}"
            lines.append(f"  attempt {a.n}  {tag}")
        if self.report:
            lines.append("")
            lines.append("  " + self.report.render().replace("\n", "\n  "))
        lines.append("")
        lines.append(f"result: {'ok' if self.ok else 'failed'}   "
                     f"tokens {self.input_tokens}in/{self.output_tokens}out")
        if self.reason:
            lines.append(self.reason)
        if self.review:
            lines.append("needs a human look: " + "; ".join(self.review))
        return "\n".join(lines)


def extract_code(text: str, entry: str = "solve") -> str:
    """Pull the code out of the reply, preferring the block with the entry function."""
    blocks = [b.strip() for b in _FENCE.findall(text)]
    for b in blocks:
        if f"def {entry}" in b:
            return b
    if blocks:
        return blocks[0]
    if f"def {entry}" in text:                     # unfenced, but it really is code
        try:
            ast.parse(text)
            return text.strip()
        except SyntaxError:
            pass
    return ""


def spec_for(requirement: str, examples: list[Example], entry: str = "solve") -> Spec:
    """Derive the spec from the requirement and examples.

    Split out because lookup needs it: the cache key includes the schema, so you need
    a spec before you can search — and a hit means you never synthesise at all.
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
    """Try at most `max_attempts` times, feeding each specific failure back in.

    Returns (code, report, failure_reason). The passing run's report is handed straight
    out — the caller used to verify again, back when the outer pass was a different,
    heavier check (hold-out plus mutation testing). The two are identical now, so a
    second pass would just be one more sandbox run.

    **The report comes out on failure too**: the layer above needs to see which cases
    the last attempt failed on, to decide whether to blame the code or the cases
    (`_blame` in jit.py). A bare reason string makes that call impossible.
    """
    feedback, last, last_rep = "", None, None
    for n in range(1, max_attempts + 1):
        user = build_user(requirement, examples, spec.param_schema, spec.return_schema, feedback)
        try:
            resp = client.complete(system=SYSTEM, user=user)
        except Refused as e:
            attempts.append(Attempt(n, "", "refused", str(e)))
            return "", None, f"model refused: {e}"

        code = extract_code(resp.text, spec.entry)
        if not code:
            attempts.append(Attempt(n, "", "no_code", "no code block in the reply",
                                    resp.input_tokens, resp.output_tokens))
            feedback = render_feedback(resp.text[:600], "no_code",
                                       "no ```python block found in the reply", {})
            last = "no_code"
            continue

        rep = verify(code, spec, examples, thresholds=th, sandbox=sb)
        if not rep.failures:
            attempts.append(Attempt(n, code, "", "all cases passed",
                                    resp.input_tokens, resp.output_tokens))
            return code, rep, ""

        g = rep.failures[0]
        attempts.append(Attempt(n, code, g.name, g.summary,
                                resp.input_tokens, resp.output_tokens))
        feedback = render_feedback(code, g.name, g.summary, g.detail)
        last, last_rep = g.name, rep

    return "", last_rep, (
        f"None of the {max_attempts} attempts passed; the last one failed at {last}. "
        "The failure itself is informative — usually it means this task is a poor fit "
        "for generated code, or the requirement and the examples contradict each other.")
