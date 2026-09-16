"""LLM clients.

Deliberately a protocol plus several implementations: one that calls the API, one that
shells out to the local CLI, one that replays canned replies. The synthesis loop has to
be testable **with no network and no API key** — otherwise every change to the loop
costs tokens and the result is not reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Haiku is the default for synthesis. It is weaker than Opus and less likely to get it
# right first time — which is exactly the pressure the repair loop should be under.
# Switching to claude-opus-5 is a one-line change; the table below absorbs the
# differences in request shape.
DEFAULT_MODEL = "claude-haiku-4-5"


@dataclass(frozen=True)
class ModelProfile:
    """Request shapes differ between model generations; one hard-coded set of
    parameters is guaranteed to 400 on some of them."""

    thinking: dict[str, Any] | None
    supports_effort: bool
    supports_temperature: bool


# Haiku 4.5 and older: thinking takes budget_tokens (< max_tokens and >= 1024), does not
#   accept output_config.effort (passing it errors), and does accept temperature.
# Opus 5 / Sonnet 5 / Opus 4.6+: thinking is adaptive, effort is accepted, and
#   **temperature has been removed — passing it is a 400**.
_PROFILES: dict[str, ModelProfile] = {
    "claude-haiku-4-5": ModelProfile({"type": "enabled", "budget_tokens": 4000}, False, True),
}
_DEFAULT_PROFILE = ModelProfile({"type": "adaptive"}, True, False)


def profile_for(model: str) -> ModelProfile:
    return _PROFILES.get(model, _DEFAULT_PROFILE)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = "end_turn"


class LLMClient(Protocol):
    def complete(self, *, system: str, user: str, max_tokens: int = 16000) -> LLMResponse:
        ...


class Refused(RuntimeError):
    """The model refused. A kind of synthesis failure — report it rather than retrying
    as if the reply were empty."""


class AnthropicClient:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high", client=None):
        import anthropic                      # deferred: the other clients must not need it

        self.model, self.effort = model, effort
        self.profile = profile_for(model)
        self._client = client or anthropic.Anthropic()

    def _params(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # `system` is identical across all three repair attempts, so it gets its own
            # cache breakpoint; `user` changes every time and is not cached
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        if t := self.profile.thinking:
            if t.get("type") == "enabled":
                t = {**t, "budget_tokens": min(t["budget_tokens"], max_tokens - 1024)}
            p["thinking"] = t
        if self.profile.supports_effort:
            p["output_config"] = {"effort": self.effort}
        return p

    def complete(self, *, system: str, user: str, max_tokens: int = 16000) -> LLMResponse:
        r = self._client.messages.create(**self._params(system, user, max_tokens))
        if r.stop_reason == "refusal":
            raise Refused(getattr(r.stop_details, "explanation", "") or "model refused")
        return LLMResponse(
            text="".join(b.text for b in r.content if b.type == "text"),
            input_tokens=r.usage.input_tokens,
            output_tokens=r.usage.output_tokens,
            cache_read_tokens=getattr(r.usage, "cache_read_input_tokens", 0) or 0,
            stop_reason=r.stop_reason or "end_turn",
        )


# The claude CLI carries its own system prompt and tool definitions on every call,
# independent of the prompt content. Measured with an empty "repeat this sentence" task:
# about 22.2k input tokens. Any token accounting based on the CLI has to subtract it,
# or synthesis looks an order of magnitude more expensive than it is — that overhead
# belongs to Claude Code, not to jitagent.
CLI_OVERHEAD_TOKENS = 22_200


@dataclass
class ClaudeCliClient:
    """Use the local `claude -p` (headless); no API key needed.

    It uses Claude Code's own authorisation, so any machine with Claude Code installed
    can synthesise without an API key.

    **Discount the numbers it reports**, in two ways:

    - `input_tokens` has the fixed overhead above subtracted. It is still an estimate:
      the CLI's system prompt changes between versions. `raw_input_tokens` keeps the
      unadjusted figure so you can reconcile.
    - Here the CLI decides `max_tokens` and the thinking budget, so the model profile
      table above does not apply. This path can measure "how many attempts does Haiku
      need", not "what would the API cost".
    """

    model: str = "haiku"
    timeout_s: int = 300
    overhead_tokens: int = CLI_OVERHEAD_TOKENS
    binary: str = "claude"
    raw_input_tokens: int = 0
    calls: int = 0

    def _argv(self, system: str) -> list[str]:
        return [
            self.binary, "-p", "--model", self.model,
            # Replace Claude Code's own system prompt with jitagent's. Without this the
            # model receives two sets of instructions that contradict each other.
            "--system-prompt", system,
            "--exclude-dynamic-system-prompt-sections",
            "--no-session-persistence",
            "--output-format", "json",
        ]

    def complete(self, *, system: str, user: str, max_tokens: int = 16000) -> LLMResponse:
        import json as _json
        import subprocess

        self.calls += 1
        try:
            p = subprocess.run(self._argv(system), input=user, capture_output=True,
                               text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI timed out after {self.timeout_s}s") from e
        if p.returncode != 0:
            raise RuntimeError(f"claude CLI exited {p.returncode}: {p.stderr[-800:]}")

        try:
            d = _json.loads(p.stdout)
        except _json.JSONDecodeError as e:
            raise RuntimeError(f"claude CLI output was not JSON: {p.stdout[:400]!r}") from e

        if d.get("is_error") or d.get("subtype") != "success":
            msg = d.get("result") or d.get("api_error_status") or d.get("subtype")
            # Report a refusal as a refusal; retrying as if the reply were empty just
            # gets refused again
            if "refus" in str(msg).lower():
                raise Refused(str(msg))
            raise RuntimeError(f"claude CLI failed: {msg}")

        u = d.get("usage") or {}
        raw_in = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                  + u.get("cache_read_input_tokens", 0))
        self.raw_input_tokens += raw_in
        return LLMResponse(
            text=d.get("result") or "",
            input_tokens=max(0, raw_in - self.overhead_tokens),
            output_tokens=u.get("output_tokens", 0),
            cache_read_tokens=u.get("cache_read_input_tokens", 0) or 0,
            stop_reason=d.get("stop_reason") or "end_turn",
        )


@dataclass
class ScriptedClient:
    """Replay canned replies in order. Used to test the synthesis loop deterministically."""

    replies: list[str]
    calls: list[dict[str, str]] = field(default_factory=list)

    def complete(self, *, system: str, user: str, max_tokens: int = 16000) -> LLMResponse:
        if len(self.calls) >= len(self.replies):
            raise AssertionError(
                f"the script has {len(self.replies)} replies but was asked for "
                f"number {len(self.calls) + 1}")
        self.calls.append({"system": system, "user": user})
        text = self.replies[len(self.calls) - 1]
        return LLMResponse(text=text, input_tokens=len(user) // 4, output_tokens=len(text) // 4)
