"""审计 agentjit 补出来的用例：**期望值到底对不对。**

见 NEXT.md 第 0 项。整套设计压在一个假设上 —— 模型补的用例期望值是对的。
一条算错的用例会把正确的实现判死，而且极难排查。

协议（重要，不然这个审计没意义）：

1. 需求、种子例子、**参考实现**三样一起写死在下面，写的时候还没见过任何生成用例。
   参考实现是本文件的 oracle：它按需求原文直译，不为任何生成结果开脱。
2. 跑 `propose_tests` 生成用例。
3. 逐条比对：参考实现算出来的 == 模型写的期望值？
4. 不一致的**逐条人工分类**，分三种：
     算错   —— 需求说清楚了，模型就是算错了
     歧义   —— 需求没说清，两种读法都站得住（那是需求的问题，不是模型的）
     参考错 —— 我的参考实现写错了

第 4 步没法自动化，所以这个脚本只负责把不一致原样打出来，人来分类。
**自动判定"错了几条"是做不到的** —— 那又会变成拿一个模型的答案验另一个模型。

    python tools/audit_tests.py            # 全部
    python tools/audit_tests.py logs rota  # 只跑指定的
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

sys.path.insert(0, "src")

from agentjit import Example                       # noqa: E402
from agentjit.llm import ClaudeCliClient           # noqa: E402
from agentjit.propose import propose_tests         # noqa: E402


@dataclass
class Task:
    key: str
    requirement: str
    seeds: list[tuple[dict, Any]]
    reference: Callable[[dict], Any]
    shape: str = ""                                 # 这个需求属于哪一类形态
    sample: Callable[[Any], dict] | None = None     # 随机造一个合法输入，端到端用
    notes: list[str] = field(default_factory=list)

    def examples(self) -> list[Example]:
        return [Example(input=i, output=o) for i, o in self.seeds]


TASKS: list[Task] = []


def task(**kw):
    def deco(fn):
        TASKS.append(Task(reference=fn, **kw))
        return fn
    return deco


def sampler(key):
    """给某个需求挂一个随机输入生成器。端到端那一步要用它造 200 个输入，
    拿参考实现当标准答案去对编译出来的代码。"""
    def deco(fn):
        next(t for t in TASKS if t.key == key).sample = fn
        return fn
    return deco


# --- 1. 文本解析 ------------------------------------------------------------
@task(
    key="logs", shape="文本解析",
    requirement=(
        "把日志行解析成结构化记录。每行形如 "
        "`2024-01-05 12:03:44 ERROR [svc-auth] 这里是消息正文`："
        "依次是日期、时间、级别、方括号里的服务名、以及剩下的全部作为消息正文。"
        "级别只可能是 DEBUG/INFO/WARN/ERROR。"
        "消息正文里可能还有方括号，以**第一个** `]` 作为服务名的结束。"
        "每行先去掉首尾空白；去掉之后为空的行直接跳过，不出现在输出里。"
        "返回 [{date, time, level, service, message}]，顺序和输入一致。"),
    seeds=[
        ({"lines": ["2024-01-05 12:03:44 ERROR [svc-auth] login failed"]},
         [{"date": "2024-01-05", "time": "12:03:44", "level": "ERROR",
           "service": "svc-auth", "message": "login failed"}]),
        ({"lines": []}, []),
    ],
)
def _logs(p):
    out = []
    for raw in p["lines"]:
        line = raw.strip()
        if not line:
            continue
        date, time, level, rest = line.split(None, 3)
        close = rest.index("]")
        out.append({"date": date, "time": time, "level": level,
                    "service": rest[1:close], "message": rest[close + 1:].strip()})
    return out


# --- 2. 日期 ---------------------------------------------------------------
@task(
    key="workdays", shape="日期计算",
    requirement=(
        "给定 start 和 end 两个日期（YYYY-MM-DD 字符串），数出这个区间里有多少个工作日。"
        "**包含 start 和 end 两个端点**。工作日指周一到周五，不考虑节假日。"
        "如果 start 比 end 晚，返回 0。返回 {days: 整数}。"),
    seeds=[
        ({"start": "2024-01-01", "end": "2024-01-05"}, {"days": 5}),   # 周一到周五
        ({"start": "2024-01-06", "end": "2024-01-07"}, {"days": 0}),   # 整个周末
    ],
)
def _workdays(p):
    a = _dt.date.fromisoformat(p["start"])
    b = _dt.date.fromisoformat(p["end"])
    if a > b:
        return {"days": 0}
    n = 0
    while a <= b:
        if a.weekday() < 5:
            n += 1
        a += _dt.timedelta(days=1)
    return {"days": n}


# --- 3. 嵌套结构 ------------------------------------------------------------
@task(
    key="flatten", shape="嵌套结构重组",
    requirement=(
        "把嵌套的 dict 压平成单层，路径上的键用点号连接，比如 "
        "`{'a': {'b': 1}}` 变成 `{'a.b': 1}`。"
        "只有 dict 继续往下展开；数组当作普通值，原样保留不展开。"
        "**空 dict 作为值时直接丢弃**，不产生任何键。"
        "返回压平后的 dict。"),
    seeds=[
        ({"obj": {"a": {"b": 1}, "c": 2}}, {"a.b": 1, "c": 2}),
        ({"obj": {}}, {}),
    ],
)
def _flatten(p):
    out = {}

    def walk(node, prefix):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(v, path)
            else:
                out[path] = v
    walk(p["obj"], "")
    return out


# --- 4. 分组 + 组内取前 N ---------------------------------------------------
@task(
    key="topn", shape="分组取前 N",
    requirement=(
        "把 {name, dept, salary} 记录按 dept 分组，每组内按 salary **从高到低**取前 2 名。"
        "薪水相同的按 name 字典序排。不足 2 人的组就有几个给几个。"
        "返回 [{dept, top}]，其中 top 是名字数组；外层按 dept 字典序升序。"),
    seeds=[
        ({"rows": [{"name": "a", "dept": "x", "salary": 10},
                   {"name": "b", "dept": "x", "salary": 20},
                   {"name": "c", "dept": "x", "salary": 15}]},
         [{"dept": "x", "top": ["b", "c"]}]),
        ({"rows": []}, []),
    ],
)
def _topn(p):
    groups: dict[str, list] = {}
    for r in p["rows"]:
        groups.setdefault(r["dept"], []).append(r)
    out = []
    for dept in sorted(groups):
        ranked = sorted(groups[dept], key=lambda r: (-r["salary"], r["name"]))
        out.append({"dept": dept, "top": [r["name"] for r in ranked[:2]]})
    return out


# --- 5. 带状态的聚合 --------------------------------------------------------
@task(
    key="uptime", shape="带状态的聚合",
    requirement=(
        "给一串事件 {ts, event}，ts 是整数秒，event 是 'start' 或 'stop'，"
        "按输入顺序处理（**不要重新排序**）。算出总运行时长（秒）。"
        "规则：start 之后遇到 stop，这一段计入总时长；"
        "已经在运行中时再遇到 start，**忽略**这个 start；"
        "不在运行中时遇到 stop，**忽略**这个 stop；"
        "最后一个 start 如果没有对应的 stop，这一段不计入。"
        "返回 {seconds: 整数}。"),
    seeds=[
        ({"events": [{"ts": 0, "event": "start"}, {"ts": 10, "event": "stop"}]},
         {"seconds": 10}),
        ({"events": []}, {"seconds": 0}),
    ],
)
def _uptime(p):
    total, running_since = 0, None
    for e in p["events"]:
        if e["event"] == "start":
            if running_since is None:
                running_since = e["ts"]
        elif e["event"] == "stop":
            if running_since is not None:
                total += e["ts"] - running_since
                running_since = None
    return {"seconds": total}


# --- 6. 舍入 / 余数分配 -----------------------------------------------------
@task(
    key="split", shape="金额分摊（舍入）",
    requirement=(
        "把 total（整数，单位是分）按 weights（正整数数组）的比例分摊。"
        "每一项先按 total * w / sum(weights) **向下取整**，"
        "然后把剩下的余数一分一分地发出去：**按权重从大到小**依次每人加 1 分，"
        "权重相同的按下标小的优先。发完为止。"
        "结果必须精确加总等于 total。返回整数数组，顺序和 weights 一致。"),
    seeds=[
        ({"total": 100, "weights": [1, 1]}, [50, 50]),
        ({"total": 10, "weights": [3, 1]}, [8, 2]),      # 7.5→7, 2.5→2, 余 1 给权重大的
    ],
)
def _split(p):
    total, ws = p["total"], p["weights"]
    s = sum(ws)
    base = [total * w // s for w in ws]
    rest = total - sum(base)
    order = sorted(range(len(ws)), key=lambda i: (-ws[i], i))
    for i in range(rest):
        base[order[i % len(order)]] += 1
    return base


# --- 随机输入生成器（端到端用）----------------------------------------------
_WORDS = ["alice", "bob", "carol", "dave", "eve", "a", "zz", "m1", "X", "svc"]


@sampler("logs")
def _s_logs(rng):
    lines = []
    for _ in range(rng.randrange(0, 6)):
        if rng.random() < 0.2:
            lines.append(rng.choice(["", "   ", "\t"]))
            continue
        lvl = rng.choice(["DEBUG", "INFO", "WARN", "ERROR"])
        msg = rng.choice(["ok", "failed [retry] later", "a b c", "]", "x]y]z", "  "])
        pad = rng.choice(["", "  ", " "])
        lines.append(f"{pad}2024-0{rng.randrange(1,10)}-1{rng.randrange(0,10)} "
                     f"1{rng.randrange(0,10)}:0{rng.randrange(0,6)}:00 {lvl} "
                     f"[{rng.choice(_WORDS)}] {msg}{pad}")
    return {"lines": lines}


@sampler("workdays")
def _s_workdays(rng):
    import datetime as d
    a = d.date(2024, 1, 1) + d.timedelta(days=rng.randrange(0, 400))
    b = a + d.timedelta(days=rng.randrange(-5, 40))
    return {"start": a.isoformat(), "end": b.isoformat()}


@sampler("flatten")
def _s_flatten(rng):
    def build(depth):
        if depth <= 0:
            return rng.choice([1, "x", None, True, [1, 2], []])
        out = {}
        for _ in range(rng.randrange(0, 4)):
            k = rng.choice(["a", "b", "c", "k1"])
            out[k] = build(depth - 1) if rng.random() < 0.5 else rng.choice(
                [1, "s", [], {}, None])
        return out
    # 顶层必须是 dict —— 需求里 obj 就是个 dict，造出标量是生成器的 bug 不是实现的
    top = build(rng.randrange(1, 4))
    return {"obj": top if isinstance(top, dict) else {}}


@sampler("topn")
def _s_topn(rng):
    return {"rows": [{"name": rng.choice(_WORDS) + str(i),
                      "dept": rng.choice(["x", "y", "z"]),
                      "salary": rng.choice([1, 5, 5, 10, 10, 20])}
                     for i in range(rng.randrange(0, 9))]}


@sampler("uptime")
def _s_uptime(rng):
    evs, ts = [], 0
    for _ in range(rng.randrange(0, 9)):
        ts += rng.randrange(0, 20)
        evs.append({"ts": ts, "event": rng.choice(["start", "stop"])})
    return {"events": evs}


@sampler("split")
def _s_split(rng):
    n = rng.randrange(1, 5)
    return {"total": rng.randrange(0, 1000),
            "weights": [rng.choice([1, 1, 2, 3, 7]) for _ in range(n)]}


# --- 跑 --------------------------------------------------------------------
def audit(t: Task, n: int = 8) -> dict:
    client = ClaudeCliClient(model="haiku")
    p = propose_tests(t.requirement, t.examples(), client=client, n=n)

    rows = []
    for e in p.examples:
        try:
            want = t.reference(e.input)
            err = None
        except Exception as ex:                    # 参考实现自己崩了也是一种不一致
            want, err = None, f"{type(ex).__name__}: {ex}"
        rows.append({"input": e.input, "model": e.output, "reference": want,
                     "note": e.note, "agree": err is None and want == e.output,
                     "ref_error": err})
    return {"key": t.key, "shape": t.shape, "requirement": t.requirement,
            "proposed": len(p.examples), "dropped": len(p.dropped),
            "error": p.error, "rows": rows,
            "tokens": [p.input_tokens, p.output_tokens]}


def main(argv):
    picked = [t for t in TASKS if not argv or t.key in argv]
    results = [audit(t) for t in picked]

    total = agree = 0
    for r in results:
        ok = sum(x["agree"] for x in r["rows"])
        total += len(r["rows"])
        agree += ok
        print(f"\n=== {r['key']}（{r['shape']}）　补了 {r['proposed']} 条，"
              f"丢弃 {r['dropped']}　一致 {ok}/{len(r['rows'])} ===")
        if r["error"]:
            print("  " + r["error"])
        for x in r["rows"]:
            if x["agree"]:
                print(f"  一致  {(x['note'] or '')[:56]}")
            else:
                print(f"  不一致 {(x['note'] or '')[:56]}")
                print(f"     输入     {json.dumps(x['input'], ensure_ascii=False)[:150]}")
                print(f"     模型写的 {json.dumps(x['model'], ensure_ascii=False)[:150]}")
                print(f"     参考实现 {json.dumps(x['reference'], ensure_ascii=False, default=str)[:150]}")
                if x["ref_error"]:
                    print(f"     参考实现崩了: {x['ref_error']}")

    print(f"\n总计：{agree}/{total} 条生成用例的期望值和参考实现一致")
    print("不一致的要人工分三类：算错 / 需求有歧义 / 参考实现错。脚本不替你分。")
    with open("tools/audit_result.json", "w") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=str)
    print("明细已写入 tools/audit_result.json")


if __name__ == "__main__":
    main(sys.argv[1:])
