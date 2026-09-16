"""Tests for ClaudeCliClient — no network; a faked subprocess is enough.

What is under test is how the CLI's JSON becomes an LLMResponse, and in particular
whether the fixed harness overhead gets subtracted. Actually driving a synthesis is
`agentjit compile --via cli`; that costs tokens and does not belong in a unit test.
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
    """The CLI carries Claude Code's own system prompt and tool definitions on every
    call. Without subtracting them, synthesis looks an order of magnitude more expensive
    than it is — that overhead belongs to Claude Code, not to agentjit."""
    r = ClaudeCliClient().complete(system="S", user="U")

    raw = 12 + 22_000 + 400
    assert r.input_tokens == raw - CLI_OVERHEAD_TOKENS
    assert r.output_tokens == 300, "output is clean; nothing to subtract"
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
    """Without replacing Claude Code's own system prompt, the model receives two sets
    of instructions that contradict each other."""
    ClaudeCliClient().complete(system="only write python code blocks", user="U")
    argv = fake["argv"]
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "only write python code blocks"
    assert "--append-system-prompt" not in argv


def test_prompt_goes_through_stdin_not_argv(fake):
    """Requirements and examples contain code blocks, quotes and newlines. Going
    through argv gets bitten by the shell or by a length limit sooner or later."""
    user = 'a"b\n```python\nx=1\n```\n' + "x" * 5000
    ClaudeCliClient().complete(system="S", user=user)
    assert fake["input"] == user
    assert user not in fake["argv"]


def test_refusal_is_reported_as_a_refusal_not_an_empty_reply(fake):
    """Retrying a refusal as if it were an empty reply just gets refused again."""
    fake["stdout"] = payload(result="refusal: I cannot do that", is_error=True, subtype="error")
    with pytest.raises(Refused):
        ClaudeCliClient().complete(system="S", user="U")


def test_garbage_output_is_an_error_with_the_output_in_it(fake):
    fake["stdout"] = "this is not JSON"
    with pytest.raises(RuntimeError, match="not JSON"):
        ClaudeCliClient().complete(system="S", user="U")
