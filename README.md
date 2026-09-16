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

## 现状：M0 验证管线已跑通

判官先到位，选手后上场 —— 先建验证管线，合成循环（接 LLM）还没写。

```bash
pip install -e . && agentjit selftest
```

```
ok   01_correct           VERIFIED   -                    正确实现应该一路绿灯
ok   02_wrong_no_clean    REJECTED   examples.visible     没做清洗，第一个例子就对不上
ok   03_crash_on_fuzz     REJECTED   fuzz.crash           五个例子全过，遇到 '-' 才炸
ok   04_overfit           REJECTED   examples.holdout     背下了可见用例，保留集立刻露馅
ok   05_dead_branch       REJECTED   coverage.branch      带着一个谁也到不了的分支
ok   06_weak_tests        REJECTED   mutation             代码对，但用例没碰过档位边界
ok   07_nondeterministic  REJECTED   determinism          返回集合序，两次结果不同
ok   08_malicious         REJECTED   static               读文件 + getattr 逃逸
ok   09_insufficient      EPHEMERAL  -                    一个例子换不来复用权
ok   10_timeout           REJECTED   examples.visible     死循环，父进程墙钟兜住
ok   11_memory_bomb       REJECTED   examples.visible     内存炸弹，看门狗兜住

11/11 个语料用例符合预期
```

每个语料用例针对一道关卡，**关卡漏报和误报都算失败** —— 一个在正确代码上误报的
关卡比漏报更难排查。这就是 M0 的出口判据：先证明判官有效，再让选手上场。

已实现的关卡：静态检查（AST 白名单）→ 判据充分性 → 例子（可见 + 保留集）→
模糊测试 → 确定性 → 分支覆盖 → 变异测试。**除合成外全程不花 token，1 秒内跑完。**

### 已知缺口

- **沙箱是正确性沙箱，不是安全沙箱。** 受限 builtins + AST 白名单 + rlimit +
  内存看门狗挡得住事故和随手的逃逸，挡不住认真的攻击者。macOS 直接忽略
  `RLIMIT_AS`，内存上限靠父进程轮询 RSS。容器/microVM 是 M2。
- **只有 T1/T5 两层 oracle。** 变形性质（T2）、差分测试（T3）、影子执行（T4）是 M1 ——
  而它们恰好是不需要标准答案的那几层。
- **变异得分门槛 80% 是拍的。** 目前语料里正确实现 95%、弱用例 72%，区分度够用，
  但这个数没有在真实数据上标定过。等价变异体也识别不了（01 唯一的存活就是个真等价变异，
  白白算进了分母）。
- **还没有合成循环。** 接 LLM 是下一步。

| 文档 | 内容 |
| --- | --- |
| [docs/design.md](docs/design.md) | 主设计（v0.2）——接口、架构、缓存、沙箱、路线图 |
| [docs/correctness.md](docs/correctness.md) | 正确性与测试——**系统能否成立的关键** |
| [docs/tracing-frontend.md](docs/tracing-frontend.md) | 自动发现重复并触发编译的前端（M4，未启动） |
| `docs/adr/` | 架构决策记录（待填） |

## 代码

```
src/agentjit/
  static_check.py   AST 白名单 —— 最便宜的一道，不过的根本不进沙箱
  sandbox.py        子进程执行 + 内存看门狗（父进程侧）
  _child.py         沙箱子进程，必须自包含
  fuzz.py           schema 随机生成 + 从真实例子扰动
  mutate.py         8 类变异算子
  holdout.py        保留集分割
  verify.py         关卡编排，便宜的先跑，第一个阻断失败就停
tests/corpus/       11 个语料用例，每个针对一道关卡
```

## 从哪读起

1. [design §4 接口设计](docs/design.md#4-接口设计) — 产品面，四个操作，其余都是实现细节
2. [correctness §2 Oracle 分层](docs/correctness.md#2-oracle-分层) — 五层判据，哪些不需要标准答案
3. [correctness §4 变形性质](docs/correctness.md#4-t2--变形性质一条性质抵一万个用例) — 一条性质抵一万个用例
4. [correctness §8 测试集够不够强](docs/correctness.md#8-测试集够不够强) — 跑过了到底能说明什么
5. [design §7.2 Facade RPC](docs/design.md#72-facade-rpc沙箱里不开洞) — 安全模型里最重要的结构决策
6. [design §10 路线图](docs/design.md#10-路线图) — M0/M1 是立项验证
