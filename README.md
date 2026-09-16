# agent-jit

一个外挂组件：**输入文本需求 → 长出代码 → 沙箱里调用 → 返回结果 → 缓存复用。**

```
compile_function("把 CSV 行按 type 分组求和", examples=[...])  → handle
call_function(handle, {rows: [...]})  × 200                    → 结果
```

第 1 次比 agent 自己算贵，第 6 次回本，第 200 次省下两个数量级。

Agent 擅长的是"想清楚要做什么"，不是"做 200 遍"。后者交给代码——这正是 JIT
对解释器做的事：热点不该每次重新推理，该编译一次然后直接跑。

## 设计上的四条主张

- **正确性只能来自模型理解之外。** 让 LLM 写测试验证 LLM 写的代码是循环论证——
  同一个误解会同时污染代码和测试，一起通过，交付一个自洽的错误。判据必须另找来源。
- **不要让调用方出题，让调用方裁决。** 独立合成几份实现，它们分歧的地方精确指出
  需求里没说清的部分。把它变成单选题，回答即成为正中要害的测试用例。
- **沙箱里一个洞都不开。** 生成的代码没有任何 I/O 能力，要做外部操作只能通过 IPC
  向宿主请求，宿主鉴权、限额、审计后代执行。能力控制集中在一处。
- **测试集是资产，代码是可再生的。** Registry 以测试集为中心，代码只是当前通过它的
  一个实现。模型升级 = 免费的全库重生成。每次线上失败都变成永久回归用例。

## 现状

设计阶段，尚无实现代码。

| 文档 | 内容 |
| --- | --- |
| [docs/design.md](docs/design.md) | 主设计（v0.2）——接口、架构、缓存、沙箱、路线图 |
| [docs/correctness.md](docs/correctness.md) | 正确性与测试——**系统能否成立的关键** |
| [docs/tracing-frontend.md](docs/tracing-frontend.md) | 自动发现重复并触发编译的前端（M4，未启动） |
| `docs/adr/` | 架构决策记录（待填） |

## 从哪读起

1. [design §4 接口设计](docs/design.md#4-接口设计) — 产品面，四个操作，其余都是实现细节
2. [correctness §2 Oracle 分层](docs/correctness.md#2-oracle-分层) — 五层判据，哪些不需要标准答案
3. [correctness §4 变形性质](docs/correctness.md#4-t2--变形性质一条性质抵一万个用例) — 一条性质抵一万个用例
4. [correctness §8 测试集够不够强](docs/correctness.md#8-测试集够不够强) — 跑过了到底能说明什么
5. [design §7.2 Facade RPC](docs/design.md#72-facade-rpc沙箱里不开洞) — 安全模型里最重要的结构决策
6. [design §10 路线图](docs/design.md#10-路线图) — M0/M1 是立项验证
