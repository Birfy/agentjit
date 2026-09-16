"""ClaudeCliClient 的测试 —— 不打网络，假一个 subprocess 就够。

这里验的是"CLI 的 JSON 怎么翻译成 LLMResponse"，尤其是那笔固定开销要不要扣。
真正跑通一次合成是 `agentjit compile --via cli`，那要花 token，不放在单测里。
"""
import json

import pytest

from agentjit.llm import CLI_OVERHEAD_TOKENS, ClaudeCliClient, Refused


class FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def payload(result="```python\ndef solve(p, c): return 1\n```", **over):
    d = {"result": result, "is_error": False, "subtype": "success",
         "stop_reason": "end_turn",
         "usage": {"input_tokens": 12, "output_tokens": 300,
                   "cache_creation_input_tokens": 22_000,
                   "cache_read_input_tokens": 400}}
    d.update(over)
    return json.dumps(d)


@pytest.fixture
def fake(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw.get("input")
        return FakeProc(seen.get("stdout", payload()))

    monkeypatch.setattr("subprocess.run", run)
    return seen


def test_the_harness_overhead_is_subtracted(fake):
    """CLI 每次都带上 Claude Code 自己的系统提示和工具定义。不扣掉的话，
    合成成本会虚高一个数量级 —— 那是 Claude Code 的开销，不是 agentjit 的。"""
    r = ClaudeCliClient().complete(system="S", user="U")

    raw = 12 + 22_000 + 400
    assert r.input_tokens == raw - CLI_OVERHEAD_TOKENS
    assert r.output_tokens == 300, "output 是干净的，不用扣"
    assert "def solve" in r.text


def test_raw_tokens_are_kept_for_reconciliation(fake):
    c = ClaudeCliClient()
    c.complete(system="S", user="U")
    c.complete(system="S", user="U")
    assert c.raw_input_tokens == 2 * (12 + 22_000 + 400) and c.calls == 2


def test_adjusted_input_never_goes_negative(fake):
    fake["stdout"] = payload(usage={"input_tokens": 1, "output_tokens": 5,
                                    "cache_creation_input_tokens": 0,
                                    "cache_read_input_tokens": 0})
    assert ClaudeCliClient().complete(system="S", user="U").input_tokens == 0


def test_system_prompt_replaces_rather_than_appends(fake):
    """不换掉 Claude Code 自己的系统提示，模型会同时收到两套互相打架的指令。"""
    ClaudeCliClient().complete(system="只写 python 代码块", user="U")
    argv = fake["argv"]
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "只写 python 代码块"
    assert "--append-system-prompt" not in argv


def test_prompt_goes_through_stdin_not_argv(fake):
    """需求和例子里有代码块、引号、换行。走 argv 迟早会被 shell 或长度限制咬。"""
    user = 'a"b\n```python\nx=1\n```\n' + "长" * 5000
    ClaudeCliClient().complete(system="S", user=user)
    assert fake["input"] == user
    assert user not in fake["argv"]


def test_refusal_is_reported_as_a_refusal_not_an_empty_reply(fake):
    """拒答当空回复重试，只会再被拒一次。"""
    fake["stdout"] = payload(result="refusal: 不能这么干", is_error=True, subtype="error")
    with pytest.raises(Refused):
        ClaudeCliClient().complete(system="S", user="U")


def test_garbage_output_is_an_error_with_the_output_in_it(fake):
    fake["stdout"] = "这不是 JSON"
    with pytest.raises(RuntimeError, match="不是 JSON"):
        ClaudeCliClient().complete(system="S", user="U")
