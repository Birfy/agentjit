"""Did the generated cases actually go after the hard parts?

`tools/audit_tests.py` answers "are the expected values right". On its own that number
means very little: a model that only writes `f([]) == []` scores 100% and has tested
nothing. **The agreement rate is only interesting once you know the cases were hard.**

So this file names the traps — the places in each requirement that are easy to misread,
written down when the requirement was — and checks **from the case's input** whether a
generated case actually exercises each one. Each trap is a predicate over the input, not
a keyword match on the note: a model can write "tests the tie-breaking" on a case with no
tie in it, and a predicate cannot be talked into anything.

    python tools/audit_tests.py && python tools/audit_traps.py
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path


# --- logs ---------------------------------------------------------------------
def _lines(p):
    return p.get("lines") or []


def _bracket_in_message(p):
    """The message body contains a `]` of its own — the trap is the requirement saying
    the **first** `]` ends the service name."""
    for raw in _lines(p):
        line = raw.strip()
        if "]" in line and "]" in line[line.index("]") + 1:]:
            return True
    return False


def _blank_line(p):
    """A line that is empty once stripped: skipped, not emitted as a record."""
    return any(not raw.strip() for raw in _lines(p))


def _untrimmed_line(p):
    """A line padded with whitespace that is not blank — stripping has to happen first."""
    return any(raw.strip() and raw != raw.strip() for raw in _lines(p))


# --- workdays -----------------------------------------------------------------
def _d(s):
    return _dt.date.fromisoformat(s)


def _start_after_end(p):
    return _d(p["start"]) > _d(p["end"])


def _weekend_endpoint(p):
    """An endpoint lands on a weekend — the trap is that both endpoints are inclusive."""
    return _d(p["start"]).weekday() >= 5 or _d(p["end"]).weekday() >= 5


def _spans_weekend(p):
    a, b = _d(p["start"]), _d(p["end"])
    if a > b:
        return False
    return any((a + _dt.timedelta(days=i)).weekday() >= 5 for i in range((b - a).days + 1))


# --- flatten ------------------------------------------------------------------
def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)


def _empty_dict_value(p):
    """An empty dict as a value: discarded, producing no key at all."""
    return any(isinstance(v, dict) and not v
               for node in _walk(p["obj"]) if isinstance(node, dict)
               for v in node.values())


def _array_value(p):
    """An array is a leaf — the trap is descending into it as if it were a dict."""
    return any(isinstance(v, list)
               for node in _walk(p["obj"]) if isinstance(node, dict)
               for v in node.values())


def _depth_3(p):
    def depth(n):
        if not isinstance(n, dict) or not n:
            return 0
        return 1 + max(depth(v) for v in n.values())
    return depth(p["obj"]) >= 3


# --- topn ---------------------------------------------------------------------
def _by_dept(p):
    g: dict[str, list] = {}
    for r in p.get("rows") or []:
        g.setdefault(r["dept"], []).append(r)
    return g


def _salary_tie(p):
    """Two people in one department on the same salary — the name tiebreak."""
    for rows in _by_dept(p).values():
        sal = [r["salary"] for r in rows]
        if len(sal) != len(set(sal)):
            return True
    return False


def _small_group(p):
    """A department with fewer than 2 people: give however many it has."""
    return any(len(rows) < 2 for rows in _by_dept(p).values())


def _multi_dept(p):
    """More than one department, so the outer ordering is actually exercised."""
    return len(_by_dept(p)) > 1


# --- uptime -------------------------------------------------------------------
def _run(p):
    """Replay the events, reporting which of the three edge conditions occurred."""
    dup_start = stop_idle = False
    running = False
    for e in p.get("events") or []:
        if e["event"] == "start":
            dup_start |= running
            running = True
        elif e["event"] == "stop":
            stop_idle |= not running
            running = False
    return dup_start, stop_idle, running


def _dup_start(p):
    return _run(p)[0]


def _stop_while_idle(p):
    return _run(p)[1]


def _trailing_start(p):
    """Ends while still running: that final stretch does not count."""
    return _run(p)[2]


# --- split --------------------------------------------------------------------
def _rest(p):
    ws, total = p["weights"], p["total"]
    s = sum(ws)
    return total - sum(total * w // s for w in ws)


def _weight_tie_at_max(p):
    ws = p["weights"]
    return len(ws) > 1 and ws.count(max(ws)) > 1


def _remainder_over_one(p):
    """More than one cent left over, so the hand-out order is actually observable."""
    return _rest(p) > 1


def _remainder_nonzero(p):
    return _rest(p) > 0


TRAPS: dict[str, list[tuple[str, object]]] = {
    "logs": [
        ("a `]` inside the message body", _bracket_in_message),
        ("a line that is blank after stripping", _blank_line),
        ("a line padded with whitespace", _untrimmed_line),
    ],
    "workdays": [
        ("start later than end", _start_after_end),
        ("an endpoint on a weekend", _weekend_endpoint),
        ("an interval spanning a weekend", _spans_weekend),
    ],
    "flatten": [
        ("an empty dict as a value", _empty_dict_value),
        ("an array as a value", _array_value),
        ("nesting three levels deep", _depth_3),
    ],
    "topn": [
        ("two people tied on salary", _salary_tie),
        ("a group with fewer than 2 people", _small_group),
        ("more than one department", _multi_dept),
    ],
    "uptime": [
        ("a start while already running", _dup_start),
        ("a stop while not running", _stop_while_idle),
        ("a final start with no stop", _trailing_start),
    ],
    "split": [
        ("the largest weight shared", _weight_tie_at_max),
        ("more than one cent left over", _remainder_over_one),
        ("any remainder at all", _remainder_nonzero),
    ],
}


def main(argv: list[str]) -> int:
    path = Path(argv[0] if argv else "tools/audit_result.json")
    if not path.exists():
        print(f"no {path} — run tools/audit_tests.py first", file=sys.stderr)
        return 1
    results = {r["key"]: r for r in json.loads(path.read_text())}

    hit = total = 0
    for key, traps in TRAPS.items():
        if key not in results:
            continue
        inputs = [row["input"] for row in results[key]["rows"]]
        print(f"\n=== {key} ===")
        for name, pred in traps:
            total += 1
            n = 0
            for i in inputs:
                try:
                    n += bool(pred(i))
                except Exception:
                    pass                       # a malformed input simply does not hit it
            hit += n > 0
            print(f"  {'hit ' if n else 'MISS'}  {name:42} {n} case(s)")

    print(f"\n{hit}/{total} traps exercised by at least one generated case")
    print("A high agreement rate only means something once this number is high too.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
