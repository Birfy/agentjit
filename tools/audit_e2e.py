"""The end-to-end audit: **is the function agentjit produces actually correct?**

The other script (audit_tests.py) checks whether the generated cases are right. This one
checks the final product, against a yardstick **the pipeline has never seen**:

    the six reference implementations in tools/audit_tests.py, transcribed literally
    from the requirement text, written before any generated case or any generated code
    existed.

Take that as the answer key and run 200 random inputs through the compiled code. This is
the question that actually matters -- **"is the function agentjit produced correct"**,
not "does the function it produced pass the tests it wrote itself".

Passing its own tests but failing here = the generated cases are too weak and missed a
real bug. Failing its own tests but passing here = a generated case had a wrong
expectation and condemned correct code. Both need to be visible.

    python tools/audit_e2e.py            # all six
    python tools/audit_e2e.py topn split # only the ones named
    python tools/audit_e2e.py --rounds 3 # several rounds each, to see the variance
"""
from __future__ import annotations

import json
import random
import sys
import time

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

from agentjit import Registry, Sandbox                     # noqa: E402
from agentjit.jit import compile_function                  # noqa: E402
from agentjit.llm import ClaudeCliClient                   # noqa: E402
from agentjit.types import deep_equal                      # noqa: E402
from audit_tests import TASKS                              # noqa: E402

N_RANDOM = 200


def judge(task, code: str, sb: Sandbox, seed: int) -> dict:
    """Run random inputs through the compiled code, with the reference as answer key."""
    rng = random.Random(seed)
    inputs = [task.sample(rng) for _ in range(N_RANDOM)]
    want = [task.reference(i) for i in inputs]

    run = sb.run(code, "solve", inputs, timeout_ms=10_000, mem_mb=512)
    if run.why_dead:
        return {"agree": 0, "n": len(inputs), "dead": run.why_dead, "diffs": []}

    diffs = []
    agree = 0
    for i, (r, w) in enumerate(zip(run.results, want)):
        if r.ok and deep_equal(r.value, w):
            agree += 1
        elif len(diffs) < 3:
            diffs.append({"input": inputs[i], "reference": w,
                          "got": r.value if r.ok else f"raised: {r.error}"})
    return {"agree": agree, "n": len(inputs), "dead": "", "diffs": diffs}


def one(task, sb, seed) -> dict:
    t0 = time.time()
    r = compile_function(task.requirement, task.examples(),
                         client=ClaudeCliClient(model="haiku"),
                         registry=Registry(f"/tmp/agentjit-e2e-4/{task.key}-{seed}"),
                         sandbox=sb, cache="force_new", name=None or "", gen_tests=8)
    row = {"key": task.key, "shape": task.shape, "seed": seed,
           "compiled": r.ok, "attempts": len(r.synth.attempts) if r.synth else 0,
           "generated": len(r.proposal.examples) if r.proposal else 0,
           "assumed": len(r.proposal.assumed) if r.proposal else 0,
           # test generation failed silently once (flatten produced 0 cases); without
           # recording this there is no way to find out why
           "propose_error": r.proposal.error if r.proposal else "",
           "propose_dropped": [d["why"] for d in r.proposal.dropped] if r.proposal else [],
           # which gate each attempt got stuck on -- the only evidence there is for
           # whether the repair loop does anything
           "gates": [a.gate or "passed" for a in r.synth.attempts] if r.synth else [],
           "summaries": [a.summary[:90] for a in r.synth.attempts] if r.synth else [],
           # keep the code from the failed attempts too -- "what did the repair
           # loop actually rescue" can only be answered from here
           "failed_code": [a.code for a in r.synth.attempts if a.gate] if r.synth else [],
           "secs": round(time.time() - t0), "reason": r.reason[:400]}
    if r.ok and r.synth:
        row.update(judge(task, r.synth.code, sb, seed + 1000))
        row["code"] = r.synth.code
    return row


def main(argv):
    rounds = 1
    if "--rounds" in argv:
        i = argv.index("--rounds")
        rounds = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    picked = [t for t in TASKS if not argv or t.key in argv]

    sb = Sandbox()
    rows = []
    for t in picked:
        for k in range(rounds):
            row = one(t, sb, k)
            rows.append(row)
            tag = ("compile failed" if not row["compiled"]
                   else f"{row['agree']}/{row['n']} agree with the reference"
                        + (f"   process died: {row['dead']}" if row.get("dead") else ""))
            print(f"{t.key:10} round {k}  {row['generated']} case(s)"
                  f" ({row['assumed']} assumed)   {row['attempts']} attempt(s)"
                  f" [{' -> '.join(row['gates'])}]   {row['secs']}s   {tag}", flush=True)
            for g, sm in zip(row["gates"], row["summaries"]):
                if g != "passed":
                    print(f"       stuck on {g}: {sm}")
            if row["propose_error"]:
                print(f"       test generation failed: {row['propose_error']}")
            for w in row["propose_dropped"][:4]:
                print(f"       dropped: {w[:100]}")
            for d in row.get("diffs", []):
                print(f"     input     {json.dumps(d['input'], ensure_ascii=False)[:110]}")
                print(f"     reference {json.dumps(d['reference'], ensure_ascii=False)[:110]}")
                print(f"     got       {json.dumps(d['got'], ensure_ascii=False, default=str)[:110]}")
            if not row["compiled"]:
                print("     " + row["reason"].replace("\n", "\n     ")[:600])

    done = [r for r in rows if r["compiled"]]
    perfect = [r for r in done if r.get("agree") == r.get("n")]
    print(f"\ncompiled {len(done)}/{len(rows)}   "
          f"of those, {len(perfect)}/{len(done)} agree with the reference on all "
          f"{N_RANDOM} random inputs")
    with open("tools/audit_e2e_result.json", "w") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2, default=str)
    print("details written to tools/audit_e2e_result.json")


if __name__ == "__main__":
    main(sys.argv[1:])
