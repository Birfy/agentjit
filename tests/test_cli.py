"""CLI 的冒烟测试。

加这个文件是因为踩过一次：`cmd_call` 里留了一行 `rt.flush()`，runtime 改成无状态
之后那个方法没了，但**没有任何测试碰过 CLI**，所以直到手动跑才发现。
CLI 是这东西的主要用法，不该只靠手测。

不打网络：用 ScriptedClient 先把函数种进 registry，再走 CLI 的读路径。
"""
import json

import pytest

from agentjit import Example, Registry, Sandbox
from agentjit.cli import main
from agentjit.llm import ScriptedClient
from agentjit.synth import compile_function

REQ = "把 {name, score} 按 score 从高到低排名次，同分并列同一名次"

EXAMPLES = [
    Example({"records": [{"name": "a", "score": 9}, {"name": "b", "score": 5}]},
            [{"name": "a", "rank": 1}, {"name": "b", "rank": 2}]),
    Example({"records": []}, [], boundary=True),
]

CODE = '''给你。

```python
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


@pytest.fixture
def home(tmp_path):
    """一个装了名为 rank 的函数的 registry，返回它的路径。"""
    reg = Registry(tmp_path / "registry")
    r = compile_function(REQ, EXAMPLES, client=ScriptedClient([CODE]), sandbox=Sandbox())
    assert r.ok, r.render()
    reg.put(REQ, r.spec, r.code, r.report, EXAMPLES, name="rank")
    return str(reg.root)


def run(home, *argv):
    return main(["--home", home, *argv])


def test_get_prints_the_code(home, capsys):
    assert run(home, "get", "rank") == 0
    assert "def solve" in capsys.readouterr().out


def test_get_on_an_unknown_name_fails_loudly(home, capsys):
    assert run(home, "get", "没这个") == 1
    assert "没有叫" in capsys.readouterr().err


def test_call_by_name(home, capsys):
    args = json.dumps({"records": [{"name": "z", "score": 1}, {"name": "y", "score": 9}]})
    assert run(home, "call", "rank", args) == 0
    out = capsys.readouterr().out
    assert "ok" in out and json.loads(out[out.index("["):]) == [
        {"name": "y", "rank": 1}, {"name": "z", "rank": 2}]


def test_call_with_a_bad_argument_is_a_guard_failure_not_a_crash(home, capsys):
    assert run(home, "call", "rank", '{"records": "不是数组"}') == 1
    assert "guard_failed" in capsys.readouterr().out


def test_list_and_inspect_show_the_name(home, capsys):
    assert run(home, "list") == 0
    assert "rank" in capsys.readouterr().out

    assert run(home, "inspect", "rank") == 0
    text = capsys.readouterr().out
    assert "rank" in text and "用例（2 个）" in text


def test_search_ranks_by_similarity(home, capsys):
    assert run(home, "search", "按 score 排名次") == 0
    assert "rank" in capsys.readouterr().out


def test_selftest_runs_the_corpus(capsys):
    assert main(["selftest"]) == 0
    assert "符合预期" in capsys.readouterr().out
