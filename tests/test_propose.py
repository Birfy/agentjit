"""自动补测试用例的测试。

这里最要紧的不是"能不能补出用例"，是**补错了会怎样**：
一条期望值写错的用例会把正确的实现判死，而且极难排查。所以下面大半篇幅在验
"什么样的生成用例会被丢掉"和"只挂在生成用例上时怎么归因"。
"""
import json

import pytest

from agentjit import Example, Registry, Sandbox
from agentjit.jit import compile_function
from agentjit.llm import LLMResponse, ScriptedClient
from agentjit.propose import propose_tests

REQ = "把 {name, score} 按 score 从高到低排名次，同分并列同一名次"

SEEDS = [
    Example({"records": [{"name": "a", "score": 9}, {"name": "b", "score": 5}]},
            [{"name": "a", "rank": 1}, {"name": "b", "rank": 2}]),
    Example({"records": []}, [], boundary=True),
]

GOOD_CODE = '''```python
def solve(params, ctx):
    rows = sorted(params["records"], key=lambda r: (-r["score"], r["name"]))
    out = []
    for i, r in enumerate(rows):
        rank = i + 1
        if i and r["score"] == rows[i - 1]["score"]:
            rank = out[-1]["rank"]
        out.append({"name": r["name"], "rank": rank})
    return out
```
'''


def fence(items):
    return "我补几条。\n\n```json\n" + json.dumps(items, ensure_ascii=False) + "\n```\n"


@pytest.fixture(scope="module")
def sb():
    return Sandbox()


def propose(reply, seeds=SEEDS, n=4):
    return propose_tests(REQ, seeds, client=ScriptedClient([reply]), n=n)


# --- 解析 ------------------------------------------------------------------
def test_parses_a_json_block():
    p = propose(fence([
        {"input": {"records": [{"name": "s", "score": 1}]},
         "output": [{"name": "s", "rank": 1}], "note": "单元素"},
        {"input": {"records": [{"name": "p", "score": 3}, {"name": "q", "score": 3}]},
         "output": [{"name": "p", "rank": 1}, {"name": "q", "rank": 1}], "note": "并列"},
    ]))
    assert [e.origin for e in p.examples] == ["generated", "generated"]
    assert p.examples[1].note == "并列"


def test_a_bare_array_without_a_fence_still_parses():
    p = propose('[{"input": {"records": []}, "output": []}]')
    # 和种子撞了，所以会被丢掉 —— 但说明解析是成功的
    assert p.error == "" and p.dropped


# --- 丢弃规则：这是防"补错"的主要手段 ----------------------------------------
def test_a_generated_case_may_not_overrule_a_seed():
    """种子是调用方给的，它们是对的。生成的用例和种子同输入不同输出，
    说明模型在改调用方的答案 —— 直接丢掉，而且要说清楚为什么。"""
    p = propose(fence([{"input": {"records": []}, "output": [{"name": "凭空", "rank": 1}]}]))
    assert p.examples == []
    assert "以调用方的为准" in p.dropped[0]["why"]


def test_malformed_items_are_dropped_not_crashed_on():
    p = propose(fence([
        {"output": [1]},                                   # 没有 input
        {"input": "不是 dict", "output": []},
        {"input": {"records": [{"name": "z", "score": 2}]},
         "output": [{"name": "z", "rank": 1}]},            # 这条是好的
    ]))
    assert len(p.examples) == 1 and len(p.dropped) == 2


def test_duplicate_inputs_are_kept_once():
    same = {"input": {"records": [{"name": "z", "score": 2}]},
            "output": [{"name": "z", "rank": 1}]}
    p = propose(fence([same, same]))
    assert len(p.examples) == 1 and len(p.dropped) == 1


def test_unserializable_expectations_are_dropped():
    p = propose(fence([{"input": {"records": []}, "output": float("nan")}]))
    assert p.examples == []


# --- 失败不该把编译带崩 ------------------------------------------------------
def test_a_reply_without_json_is_reported_not_raised():
    p = propose("我觉得这个需求不用测试。")
    assert p.examples == [] and "找不到 JSON" in p.error


def test_a_broken_client_does_not_take_the_whole_compile_down():
    """补用例是锦上添花。它挂了应该退回"只用调用方给的用例"，而不是整个编译失败。"""
    class Broken:
        def complete(self, **kw):
            raise RuntimeError("网络没了")

    p = propose_tests(REQ, SEEDS, client=Broken(), n=4)
    assert p.examples == [] and "网络没了" in p.error


# --- 归因：整套设计里最要紧的一条 --------------------------------------------
def test_failing_only_on_generated_cases_is_handed_back_for_adjudication(tmp_path, sb):
    """代码通过了调用方的全部用例，只挂在自动补的用例上。

    这时候**说不准是代码错了还是用例错了**，不能报成"代码有 bug" ——
    拿一条错用例否决正确的代码，比漏个 bug 难查得多。
    """
    # 这条生成用例的期望值是错的：同分并列，两个 3 分都该是 rank 1
    wrong = fence([{"input": {"records": [{"name": "p", "score": 3},
                                          {"name": "q", "score": 3}]},
                    "output": [{"name": "p", "rank": 1}, {"name": "q", "rank": 2}],
                    "note": "并列（这条其实写错了）"}])
    client = ScriptedClient([wrong] + [GOOD_CODE] * 3)

    r = compile_function(REQ, SEEDS, client=client, registry=Registry(tmp_path / "r"),
                         sandbox=sb, gen_tests=1)

    assert not r.ok
    assert "通过了**你给的全部用例**" in r.reason
    assert "请裁决" in r.reason and "gen_tests=0" in r.reason


def test_failing_on_a_caller_case_is_still_plainly_the_code_s_fault(tmp_path, sb):
    """调用方的用例挂了就是代码错了，不该被"可能是用例错了"这套话术稀释。"""
    bad_code = '```python\ndef solve(params, ctx):\n    return []\n```\n'
    client = ScriptedClient([fence([])] + [bad_code] * 3)

    r = compile_function(REQ, SEEDS, client=client, registry=Registry(tmp_path / "r"),
                         sandbox=sb, gen_tests=1)

    assert not r.ok
    assert "通过了**你给的全部用例**" not in r.reason
    assert "3 次尝试都没通过" in r.reason


# --- 串起来 ----------------------------------------------------------------
def test_generated_cases_land_in_the_test_set_and_are_marked(tmp_path, sb):
    extra = fence([{"input": {"records": [{"name": "p", "score": 3},
                                          {"name": "q", "score": 3}]},
                    "output": [{"name": "p", "rank": 1}, {"name": "q", "rank": 1}],
                    "note": "并列"}])
    reg = Registry(tmp_path / "r")
    r = compile_function(REQ, SEEDS, client=ScriptedClient([extra, GOOD_CODE]),
                         registry=reg, sandbox=sb, name="rank", gen_tests=1)
    assert r.ok, r.render()

    origins = [e.origin for e in reg.get("rank").tests.examples]
    assert origins == ["caller", "caller", "generated"], \
        "生成的用例要进测试集，而且要留着出处 —— 下次换模型重生成时它们一起当验收标准"


def test_gen_tests_zero_skips_the_extra_call(tmp_path, sb):
    client = ScriptedClient([GOOD_CODE])
    r = compile_function(REQ, SEEDS, client=client, registry=Registry(tmp_path / "r"),
                         sandbox=sb, gen_tests=0)
    assert r.ok and len(client.calls) == 1 and r.proposal is None


def test_tokens_include_the_test_writing_call(tmp_path, sb):
    r = compile_function(REQ, SEEDS, client=ScriptedClient([fence([]), GOOD_CODE]),
                         registry=Registry(tmp_path / "r"), sandbox=sb, gen_tests=1)
    assert r.ok
    assert r.tokens[0] > r.synth.input_tokens, "补用例那次调用的 token 也得算进去"
