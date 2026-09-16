"""Audit the cases agentjit writes: **are the expected values actually right?**

The whole design rests on one assumption — that the expectations the model writes into
its cases are correct. A case with a wrong expected value condemns a correct
implementation, and it is very hard to debug.

The protocol matters; without it the audit means nothing:

1. The requirement, the seed examples and the **reference implementation** are all
   written down here together, before seeing a single generated case. The reference is
   this file's oracle: a literal transcription of the requirement text, which makes no
   excuses for any generated result.
2. Run `propose_tests` to generate cases.
3. Compare one by one: does the reference's answer equal the model's expected value?
4. **Classify every disagreement by hand**, into three kinds:
     wrong      — the requirement was clear and the model got it wrong
     ambiguous  — the requirement did not say, and both readings stand up (that is the
                  requirement's problem, not the model's)
     bad oracle — the reference implementation here is the one that is wrong

Step 4 cannot be automated, so this script only prints the disagreements verbatim and
leaves the classification to a person. **Deciding "how many are wrong" automatically is
not possible** — that would just be checking one model's answers with another model's.

    python tools/audit_tests.py            # everything
    python tools/audit_tests.py logs topn  # only the ones named
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
    shape: str = ""                                 # which family of problem this is
    sample: Callable[[Any], dict] | None = None     # build a random legal input (e2e)
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
    """Attach a random-input generator to a requirement. The end-to-end step uses it to
    build 200 inputs and checks the compiled code against the reference."""
    def deco(fn):
        next(t for t in TASKS if t.key == key).sample = fn
        return fn
    return deco


# --- 1. text parsing ------------------------------------------------------------
@task(
    key="logs", shape="text parsing",
    requirement=(
        "Parse log lines into structured records. Each line looks like "
        "`2024-01-05 12:03:44 ERROR [svc-auth] the message body goes here`: "
        "a date, a time, a level, a service name in square brackets, and then "
        "everything that is left as the message body. "
        "The level is one of DEBUG/INFO/WARN/ERROR. "
        "The message body may contain square brackets of its own, so the **first** "
        "`]` is what ends the service name. "
        "Strip leading and trailing whitespace from each line first; a line that is "
        "empty after stripping is skipped and does not appear in the output. "
        "Return [{date, time, level, service, message}] in the input order."),
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


# --- 2. dates ---------------------------------------------------------------
@task(
    key="workdays", shape="date arithmetic",
    requirement=(
        "Given two dates start and end (YYYY-MM-DD strings), count the working days in "
        "the interval. **Both endpoints are included.** A working day is Monday to "
        "Friday; holidays are not considered. "
        "If start is later than end, return 0. Return {days: integer}."),
    seeds=[
        ({"start": "2024-01-01", "end": "2024-01-05"}, {"days": 5}),   # Mon to Fri
        ({"start": "2024-01-06", "end": "2024-01-07"}, {"days": 0}),   # a whole weekend
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


# --- 3. nested structures ------------------------------------------------------------
@task(
    key="flatten", shape="restructuring nested data",
    requirement=(
        "Flatten a nested dict into a single level, joining the keys along each path "
        "with a dot, so `{'a': {'b': 1}}` becomes `{'a.b': 1}`. "
        "Only a dict is descended into; an array counts as an ordinary value and is "
        "kept as it is, not expanded. "
        "**An empty dict used as a value is discarded** and produces no key at all. "
        "Return the flattened dict."),
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


# --- 4. group, then take the top N in each group ---------------------------------------------------
@task(
    key="topn", shape="top N per group",
    requirement=(
        "Group {name, dept, salary} records by dept and take the top 2 in each group by "
        "salary, **highest first**. "
        "Records on the same salary are ordered by name, lexicographically. A group "
        "with fewer than 2 people gives however many it has. "
        "Return [{dept, top}], where top is an array of names; the outer array is "
        "sorted by dept, lexicographically ascending."),
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


# --- 5. stateful aggregation --------------------------------------------------------
@task(
    key="uptime", shape="stateful aggregation",
    requirement=(
        "Given a series of events {ts, event}, where ts is an integer number of seconds "
        "and event is either 'start' or 'stop', process them in the input order "
        "(**do not re-sort them**) and compute the total running time in seconds. "
        "The rules: a stop that follows a start closes that stretch and it counts "
        "towards the total; "
        "a start that arrives while already running is **ignored**; "
        "a stop that arrives while not running is **ignored**; "
        "and a final start with no matching stop does not count. "
        "Return {seconds: integer}."),
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


# --- 6. rounding and remainder distribution -----------------------------------------------------
@task(
    key="split", shape="splitting an amount (rounding)",
    requirement=(
        "Split total (an integer number of cents) in proportion to weights (an array of "
        "positive integers). "
        "Each item first gets total * w / sum(weights), **rounded down**. "
        "Then hand out the remaining cents one at a time: **in descending order of "
        "weight**, give each item one more cent in turn, breaking a tie in weight by "
        "the lower index, until the remainder is gone. "
        "The result must sum to exactly total. Return an array of integers in the same "
        "order as weights."),
    seeds=[
        ({"total": 100, "weights": [1, 1]}, [50, 50]),
        # 7.5 -> 7, 2.5 -> 2, and the 1 left over goes to the larger weight
        ({"total": 10, "weights": [3, 1]}, [8, 2]),
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


# --- random input generators (used by the end-to-end audit) ----------------------------------------------
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
    # the top level has to be a dict — the requirement says obj is one, so producing a
    # scalar here would be a bug in the generator, not in the implementation
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


# --- running the audit --------------------------------------------------------------------
def audit(t: Task, n: int = 8) -> dict:
    client = ClaudeCliClient(model="haiku")
    p = propose_tests(t.requirement, t.examples(), client=client, n=n)

    rows = []
    for e in p.examples:
        try:
            want = t.reference(e.input)
            err = None
        except Exception as ex:                    # the reference blowing up is also
                                                   # a disagreement
            want, err = None, f"{type(ex).__name__}: {ex}"
        rows.append({"input": e.input, "model": e.output, "reference": want,
                     "note": e.note, "agree": err is None and want == e.output,
                     # An assumption the model made where the requirement said nothing.
                     # Worth recording: a case resting on one is not a case anybody can be
                     # said to be wrong about, so it changes how a failure is attributed.
                     "assumes": e.assumes,
                     "ref_error": err})
    return {"key": t.key, "shape": t.shape, "requirement": t.requirement,
            "proposed": len(p.examples), "dropped": len(p.dropped),
            "assumed": len(p.assumed),
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
        print(f"\n=== {r['key']} ({r['shape']})   wrote {r['proposed']}, "
              f"dropped {r['dropped']}, {r['assumed']} assumed   "
              f"agree {ok}/{len(r['rows'])} ===")
        if r["error"]:
            print("  " + r["error"])
        for x in r["rows"]:
            if x["agree"]:
                print(f"  agree      {(x['note'] or '')[:56]}")
                if x["assumes"]:
                    print(f"     ! assumes {x['assumes'][:80]}")
            else:
                print(f"  DISAGREE   {(x['note'] or '')[:56]}")
                print(f"     input      {json.dumps(x['input'], ensure_ascii=False)[:150]}")
                print(f"     model      {json.dumps(x['model'], ensure_ascii=False)[:150]}")
                print(f"     reference  {json.dumps(x['reference'], ensure_ascii=False, default=str)[:150]}")
                if x["ref_error"]:
                    print(f"     the reference blew up: {x['ref_error']}")

    print(f"\n{agree}/{total} generated expectations agree with the reference")
    print("Classify each disagreement by hand: wrong / ambiguous / bad oracle. "
          "This script does not do it for you.")
    with open("tools/audit_result.json", "w") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=str)
    print("details written to tools/audit_result.json")


if __name__ == "__main__":
    main(sys.argv[1:])
