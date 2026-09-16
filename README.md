# agent-jit

把 agent 在长程任务里反复"解释执行"的子任务，编译成可复用、可验证、可失效的代码单元。

Agent 的执行循环本质上是解释器：每一步 tool 调用的决策都要把上下文重新喂进模型推理一遍。
同一个子任务做 200 遍，就付 200 遍的推理成本，还要承受 200 次重新犯错的机会。

编译器领域对这个问题有成熟解法——JIT。agent-jit 把它搬过来：

```
热点探测 → 轨迹反合一 → LLM 合成代码 → 多级验证 → 带 guard 执行 → guard 失败则 deopt 回退
```

关键性质是 **deopt**：编译路径的假设一旦被打破，立刻回退到 LLM 路径。
最坏情况只是退化成今天的 agent——慢一点，但不会错。

## 现状

设计阶段，尚无实现代码。

- [docs/design.md](docs/design.md) — 主设计文档
- `docs/adr/` — 架构决策记录（待填）

## 从哪读起

1. [§2 核心类比](docs/design.md#2-核心类比) — 整个设计的骨架
2. [§6.1 部分编译](docs/design.md#61-部分编译不是所有步骤都能变成代码) — 与"缓存 prompt / 存 playbook"的根本区别
3. [§7 Guard 与 Deoptimization](docs/design.md#7-guard-与-deoptimization) — 正确性从哪来
4. [§13 路线图](docs/design.md#13-路线图) — M0/M1 是立项验证
