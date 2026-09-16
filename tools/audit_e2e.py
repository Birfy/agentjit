"""端到端审计：**agentjit 产出的函数，到底对不对。**

前一个脚本（audit_tests.py）验的是"补出来的用例对不对"。这个验的是最终产物 ——
而且用的是一个**管道完全没见过**的判据：

    tools/audit_tests.py 里那 6 个参考实现，我按需求原文直译写的，
    写的时候没见过任何生成用例、也没见过任何生成代码。

拿它当标准答案，对编译出来的代码跑 200 个随机输入。这才是真正要回答的问题：
**"agentjit 产出的函数正确吗"**，而不是"它产出的函数能通过它自己写的测试吗"。

后者过了前者不过 = 生成的用例太弱，漏掉了真 bug。
前者过了后者不过 = 生成的用例算错了，把正确代码判死了。两种都要看得见。

    python tools/audit_e2e.py            # 全部 6 个
    python tools/audit_e2e.py topn split # 只跑指定的
    python tools/audit_e2e.py --rounds 3 # 同一个需求跑几轮，看方差
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
    """拿参考实现当标准答案，对编译出来的代码跑随机输入。"""
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
                          "got": r.value if r.ok else f"崩了: {r.error}"})
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
           # 补用例那步静默失败过一次（flatten 补出 0 条），不记下来就查不到原因
           "propose_error": r.proposal.error if r.proposal else "",
           "propose_dropped": [d["why"] for d in r.proposal.dropped] if r.proposal else [],
           # 每次尝试卡在哪道关卡 —— 这是"修复循环到底有没有用"的唯一证据
           "gates": [a.gate or "通过" for a in r.synth.attempts] if r.synth else [],
           "summaries": [a.summary[:90] for a in r.synth.attempts] if r.synth else [],
           # 挂掉那几次的代码也留着 —— "修复循环救回了什么"只能从这里看
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
            tag = (f"编译失败" if not row["compiled"]
                   else f"{row['agree']}/{row['n']} 和参考实现一致"
                        + (f"　进程死了: {row['dead']}" if row.get("dead") else ""))
            print(f"{t.key:10} 轮{k}  补{row['generated']}条"
                  f"（{row['assumed']}条带假设）　尝试{row['attempts']}次"
                  f" [{' → '.join(row['gates'])}]　{row['secs']}s　{tag}", flush=True)
            for g, sm in zip(row["gates"], row["summaries"]):
                if g != "通过":
                    print(f"       卡在 {g}: {sm}")
            if row["propose_error"]:
                print(f"       补用例出错: {row['propose_error']}")
            for w in row["propose_dropped"][:4]:
                print(f"       丢弃: {w[:100]}")
            for d in row.get("diffs", []):
                print(f"     输入 {json.dumps(d['input'], ensure_ascii=False)[:110]}")
                print(f"     参考 {json.dumps(d['reference'], ensure_ascii=False)[:110]}")
                print(f"     实际 {json.dumps(d['got'], ensure_ascii=False, default=str)[:110]}")
            if not row["compiled"]:
                print("     " + row["reason"].replace("\n", "\n     ")[:600])

    done = [r for r in rows if r["compiled"]]
    perfect = [r for r in done if r.get("agree") == r.get("n")]
    print(f"\n编译成功 {len(done)}/{len(rows)}　"
          f"其中和参考实现 200/200 一致的：{len(perfect)}/{len(done)}")
    with open("tools/audit_e2e_result.json", "w") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2, default=str)
    print("明细写入 tools/audit_e2e_result.json")


if __name__ == "__main__":
    main(sys.argv[1:])
