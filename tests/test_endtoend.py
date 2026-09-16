"""The loop closed: a sentence -> synthesis -> storage -> fetch by name / call again.

A scripted client throughout: no network, no tokens. What this proves is that the pipe
is connected, not "how many attempts Haiku really needs".
"""
import pytest

from jitagent import Example, Registry, Runtime, Sandbox, get_code
from jitagent.llm import ScriptedClient
from jitagent.synth import compile_function

REQ = ("Group rows by type and sum the amounts, returning {type: total}. "
       "An amount may carry a currency symbol and thousands separators; "
       "an empty one counts as 0.")

EXAMPLES = [
    Example({"rows": [{"type": "refund", "amount": "$1,200.50"},
                      {"type": "sale", "amount": "$300"}],},
            {"refund": 1200.5, "sale": 300.0}),
    Example({"rows": []}, {}, boundary=True),
    Example({"rows": [{"type": "sale", "amount": ""}]}, {"sale": 0.0}, boundary=True),
    Example({"rows": [{"type": "sale", "amount": "$100"},
                      {"type": "sale", "amount": "$50"}]}, {"sale": 150.0}),
    Example({"rows": [{"type": "fee", "amount": "-$25.50"}]}, {"fee": -25.5}, boundary=True),
]

CORRECT = '''Clean each value first, then accumulate.

```python
def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        raw = row.get("amount") or ""
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
        try:
            value = float(cleaned)
        except ValueError:
            value = 0.0
        totals[row["type"]] = totals.get(row["type"], 0.0) + value
    return totals
```
'''


@pytest.fixture(scope="module")
def sb():
    return Sandbox()


def test_compile_then_get_and_call_by_name(tmp_path, sb):
    reg = Registry(tmp_path / "registry")
    rt = Runtime(reg, sb)

    r = compile_function(REQ, EXAMPLES, client=ScriptedClient([CORRECT]), sandbox=sb)
    assert r.ok, r.render()
    fn = reg.put(REQ, r.spec, r.code, r.report, EXAMPLES, name="group_sum",
                 model="scripted", attempts=len(r.attempts))

    # fetch the code by name
    assert "def solve" in get_code("group_sum", registry=reg)
    assert reg.get("group_sum").spec_hash == fn.spec_hash

    # call it repeatedly by name
    args = {"rows": [{"type": "sale", "amount": f"${i}"} for i in range(1, 6)]}
    outs = rt.call_many("group_sum", [args] * 200)
    assert all(o.ok for o in outs)
    assert outs[0].result == {"sale": 15.0}

    # what lands on disk is the cases, not just the code
    back = reg.get("group_sum")
    assert len(back.tests.examples) == len(EXAMPLES)
    assert back.best().code.strip() == r.code.strip()
    assert back.best().report.level.value == "VERIFIED"



def test_recompiling_the_same_requirement_adds_a_version_not_a_function(tmp_path, sb):
    """Compiling the same requirement again should grow a v2, not a second function —
    otherwise the test set forks into two copies and "a better model means a free
    regeneration of everything" loses the thing it rests on."""
    reg = Registry(tmp_path / "registry")
    for _ in range(2):
        r = compile_function(REQ, EXAMPLES, client=ScriptedClient([CORRECT]), sandbox=sb)
        fn = reg.put(REQ, r.spec, r.code, r.report, EXAMPLES)

    assert len(reg.all()) == 1
    assert [v.name for v in reg.get(fn.handle).versions] == ["v1", "v2"]
    assert len(reg.get(fn.handle).tests.examples) == len(EXAMPLES)
