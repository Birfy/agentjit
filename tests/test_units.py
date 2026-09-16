"""组件级单测。语料用例（agentjit selftest）验的是端到端行为，这里验的是零件。"""
import pytest

from agentjit import Example, Sandbox, Spec, verify
from agentjit.static_check import check
from agentjit.types import deep_equal

CODE = "def solve(params, ctx):\n    return {'n': len(params['rows'])}\n"


# --- 静态检查 --------------------------------------------------------------
@pytest.mark.parametrize("src, needle", [
    ("import os\ndef solve(params, ctx): return {}", "禁止 import"),
    ("def solve(params, ctx): return open('/etc/passwd').read()", "禁止使用 open"),
    ("def solve(params, ctx): return eval('1')", "禁止使用 eval"),
    ("def solve(params, ctx): return params.__class__", "dunder 属性"),
    ("def solve(params, ctx): return getattr(params, 'x')", "禁止使用 getattr"),
    # getattr 被挡住后，dunder 字面量也要挡 —— 否则换个写法就绕过去了
    ("def solve(params, ctx): return '__class__'", "dunder 字面量"),
    ("def solve(params, ctx): return {}\nAPI_KEY = 'sk-abcdefghijklmnop'", "凭据"),
    ("def solve(params): return {}", "恰好两个参数"),
    ("def other(params, ctx): return {}", "缺少入口函数"),
    ("def solve(params, ctx) return {}", "语法错误"),
])
def test_static_check_rejects(src, needle):
    assert any(needle in v for v in check(src)), check(src)


def test_static_check_accepts_clean_code():
    assert check(CODE) == []


def test_injected_modules_need_no_import():
    src = "def solve(params, ctx):\n    return {'n': len(re.findall(r'\\d', params['s']))}\n"
    assert check(src) == []


# --- 沙箱 ------------------------------------------------------------------
@pytest.fixture(scope="module")
def sb():
    return Sandbox()


def test_sandbox_runs_and_batches(sb):
    r = sb.run(CODE, "solve", [{"rows": []}, {"rows": [1, 2]}])
    assert r.all_ok and [x.value for x in r.results] == [{"n": 0}, {"n": 2}]


def test_sandbox_isolates_failures_per_call(sb):
    src = "def solve(params, ctx):\n    return {'v': 1 / params['d']}\n"
    r = sb.run(src, "solve", [{"d": 2}, {"d": 0}])
    assert r.ok and r.results[0].ok and not r.results[1].ok
    assert "ZeroDivisionError" in r.results[1].error


def test_sandbox_kills_infinite_loop(sb):
    r = sb.run("def solve(params, ctx):\n    while True:\n        pass\n",
               "solve", [{}], timeout_ms=800)
    assert r.killed == "timeout" and r.why_dead == "执行超时"


def test_sandbox_contains_memory_bomb(sb):
    """两条路都算兜住，测的是"兜住了"而不是"谁兜住的"。

    Linux 上 RLIMIT_AS 生效，子进程自己抛 MemoryError，那一次调用失败但进程还在；
    Darwin 忽略 RLIMIT_AS，只能靠父进程的看门狗轮询 RSS 再 kill。
    钉死其中一条，另一个平台上就会红。
    """
    r = sb.run("def solve(params, ctx):\n    return {'n': len([0] * 200000000)}\n",
               "solve", [{}], mem_mb=256, timeout_ms=15000)
    caught_by_rlimit = bool(r.results) and "MemoryError" in r.results[0].error
    assert r.killed == "memory" or caught_by_rlimit, r


def test_sandbox_swallows_stdout_from_generated_code(sb):
    # 被测代码往 stdout 写东西不能污染 JSON 协议
    src = "def solve(params, ctx):\n    json.dump({'x': 1}, sys.stdout) if False else None\n    return {'ok': 1}\n"
    r = sb.run(src, "solve", [{}])
    assert r.all_ok and r.results[0].value == {"ok": 1}


def test_sandbox_rejects_unserializable_return(sb):
    r = sb.run("def solve(params, ctx):\n    return {'s': {1, 2}}\n", "solve", [{}])
    assert not r.results[0].ok


# --- 比较 ------------------------------------------------------------------
@pytest.mark.parametrize("a, b, want", [
    (0.1 + 0.2, 0.3, True),          # 浮点容差：要求 bit 相等会把正确实现判错
    ({"a": 1}, {"a": 1.0}, True),
    ({"a": 1}, {"a": 2}, False),
    (True, 1, False),                # bool 不等于 int
    ([1, 2], [2, 1], False),
])
def test_deep_equal(a, b, want):
    assert deep_equal(a, b) is want


# --- 端到端 ----------------------------------------------------------------
def test_verify_short_circuits_on_static_failure():
    spec = Spec(intent="x", param_schema={"type": "object"}, return_schema={"type": "object"})
    r = verify("import os\ndef solve(params, ctx): return {}", spec,
               [Example(input={}, output={})])
    assert r.level.value == "REJECTED"
    assert [g.name for g in r.gates] == ["static"]      # 没进沙箱




# --- 沙箱的已知限制 --------------------------------------------------------
def test_strptime_is_known_broken_and_the_prompt_says_so(sb):
    """`datetime.datetime.strptime` 在沙箱里用不了：它第一次调用时才 import
    `_strptime`，而受限 builtins 里没有 `__import__`。

    这条是实测撞出来的 —— 模型写的日期逻辑完全正确，被它挡下来，白烧一轮合成
    去改写。修沙箱要往 builtins 里放 `__import__`，和"沙箱里一个洞都不开"冲突，
    所以改成在 prompt 里告诉模型别用。实测：合成从 2~3 次尝试降到 1 次。

    这个测试钉的是**两件事必须同步**：限制还在，prompt 里就得写着。
    哪天沙箱能跑 strptime 了，这条会红 —— 那时候把 prompt 里那段删掉。
    """
    from agentjit.prompts import SYSTEM

    src = ('def solve(params, ctx):\n'
           '    return {"v": datetime.datetime.strptime(params["s"], "%Y-%m-%d").year}\n')
    r = sb.run(src, "solve", [{"s": "2024-01-05"}])
    assert not r.results[0].ok and "__import__" in r.results[0].error
    assert "strptime" in SYSTEM, "限制还在，prompt 里就必须写着"

    # 推荐的替代写法必须真的能用，否则等于把模型指到另一个坑里
    ok = sb.run('def solve(params, ctx):\n'
                '    return {"v": datetime.date.fromisoformat(params["s"]).year}\n',
                "solve", [{"s": "2024-01-05"}])
    assert ok.results[0].value == {"v": 2024}
