"""LLM 客户端。

刻意做成协议 + 两个实现：真客户端打 API，脚本客户端回放预设答案。
合成循环本身的正确性要能在**没有网络、没有 API key** 的情况下测 ——
否则每次改循环逻辑都得烧 token，而且测不出确定的结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# 默认用 Haiku 跑合成。它比 Opus 弱，一次写对的概率低 —— 这恰好是修复循环
# 该被压力测试的地方。换成 claude-opus-5 只要改这一行（能力差异由下面的表吸收）。
DEFAULT_MODEL = "claude-haiku-4-5"


@dataclass(frozen=True)
class ModelProfile:
    """各代模型的请求形状不一样，写死一套参数必然在某个模型上 400。"""

    thinking: dict[str, Any] | None
    supports_effort: bool
    supports_temperature: bool


# Haiku 4.5 / 更老的模型：thinking 用 budget_tokens（须 < max_tokens 且 >= 1024），
#   不支持 output_config.effort（传了报错），支持 temperature。
# Opus 5 / Sonnet 5 / Opus 4.6+：thinking 用 adaptive，支持 effort，
#   **temperature 已移除，传了直接 400**。
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
    """模型拒答。合成失败的一种，要如实报出来而不是当成空回复重试。"""


class AnthropicClient:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high", client=None):
        import anthropic                      # 延迟导入：脚本客户端不该依赖它

        self.model, self.effort = model, effort
        self.profile = profile_for(model)
        self._client = client or anthropic.Anthropic()

    def _params(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            # system 在三次修复之间一字不变，单独打 cache 断点；user 每次都变，不缓存
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
            raise Refused(getattr(r.stop_details, "explanation", "") or "模型拒答")
        return LLMResponse(
            text="".join(b.text for b in r.content if b.type == "text"),
            input_tokens=r.usage.input_tokens,
            output_tokens=r.usage.output_tokens,
            cache_read_tokens=getattr(r.usage, "cache_read_input_tokens", 0) or 0,
            stop_reason=r.stop_reason or "end_turn",
        )


# claude CLI 每次调用都会带上它自己的系统提示和工具定义，和 prompt 内容无关。
# 实测（拿一个"复读一句话"的空任务量的）约 22.2k input token。拿 CLI 量出来的数
# 去算 net_savings 必须把它减掉，否则合成成本虚高一个数量级 —— 那是 Claude Code
# 的开销，不是 agentjit 的。
CLI_OVERHEAD_TOKENS = 22_200


@dataclass
class ClaudeCliClient:
    """走本机的 `claude -p`（headless），不需要 API key。

    用的是 Claude Code 自己的授权，所以装了 Claude Code 的机器就能合成 ——
    这是没有 API 凭据时唯一能跑通 NEXT.md 第 0 项的路。

    **量出来的数要打个折扣**，两处：

    - `input_tokens` 扣掉了 `overhead_tokens` 的固定开销（见上）。扣完仍然是估的，
      因为 CLI 的系统提示会随版本变。`raw_input_tokens` 留着原始数好对账。
    - 这条路上 `max_tokens` 和 thinking 预算都由 CLI 决定，`llm.py` 的模型能力表
      在这里不起作用。所以它测得出"Haiku 几次能修对"，测不出"直连 API 要花多少钱"。
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
            # 换掉 Claude Code 自己的系统提示，只留 agentjit 的 —— 不换的话
            # 模型会同时收到两套互相打架的指令
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
            raise RuntimeError(f"claude CLI 超时（{self.timeout_s}s）") from e
        if p.returncode != 0:
            raise RuntimeError(f"claude CLI 退出码 {p.returncode}：{p.stderr[-800:]}")

        try:
            d = _json.loads(p.stdout)
        except _json.JSONDecodeError as e:
            raise RuntimeError(f"claude CLI 的输出不是 JSON：{p.stdout[:400]!r}") from e

        if d.get("is_error") or d.get("subtype") != "success":
            msg = d.get("result") or d.get("api_error_status") or d.get("subtype")
            # 拒答要如实报成拒答，不能当空回复重试 —— 重试只会再被拒一次
            if "refus" in str(msg).lower():
                raise Refused(str(msg))
            raise RuntimeError(f"claude CLI 失败：{msg}")

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
    """按顺序回放预设回复。用来确定性地测合成循环本身。"""

    replies: list[str]
    calls: list[dict[str, str]] = field(default_factory=list)

    def complete(self, *, system: str, user: str, max_tokens: int = 16000) -> LLMResponse:
        if len(self.calls) >= len(self.replies):
            raise AssertionError(
                f"脚本只准备了 {len(self.replies)} 条回复，被要了第 {len(self.calls) + 1} 条")
        self.calls.append({"system": system, "user": user})
        text = self.replies[len(self.calls) - 1]
        return LLMResponse(text=text, input_tokens=len(user) // 4, output_tokens=len(text) // 4)
