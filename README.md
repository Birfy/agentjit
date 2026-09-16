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

## 现状：M0 已跑通

判官先到位，选手后上场 —— 先建验证管线，再接合成循环。两部分都能独立测：
验证管线靠人为植入错误的语料，合成循环靠回放脚本（不打网络、不花 token）。

```bash
pip install -e .
agentjit selftest                        # 验证管线：11 个语料用例
pytest                                   # 40 个单测，含合成循环全流程
agentjit compile examples/rank.json      # 真的合成一个函数（要 API 凭据）
```

### 合成循环

```
生成 → 静态检查 → 跑可见用例 → 模糊 → 确定性 → 覆盖率 → 结构化反馈 → 再生成
                                                              ↓ 至多三次
                                          终审：全部用例 + 保留集 + 变异测试
```

两条纪律：

- **修复循环只能看见可见用例。** 保留集对它完全不可见。轮换保留集只允许一次，
  而且是防"运气不好的分割"，不是给模型再看一眼的机会。
  *但覆盖率是个例外*：它问的是"整个测试集有没有验证到这段代码"，拿可见用例量会
  逼模型删掉只有保留集才走得到的必要分支。覆盖率只需要输入不需要答案，所以用全部
  输入量、只用可见用例判对错，反馈里也只说"第几行没覆盖"。
- **不是所有失败都该反馈给模型。** 崩溃、不确定、死分支是代码问题，反馈回去能修；
  变异得分低是**测试集**问题 —— 反馈回去只会让模型扭曲代码去迎合弱用例。后者如实
  报给调用方："你的用例不够，缺的正是这几处。"

反馈是结构化的：`输入 X / 期望 Y / 实际 Z / traceback`，不是"没通过，再试试"。

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

- **真实 LLM 合成还没跑通过一次。** 循环逻辑全部由回放脚本覆盖，但本机的
  OAuth 凭据已过期（`ant auth login` 可修），所以还没有一条真实的端到端记录。
  在拿到之前，"Haiku 几次能修对"这个数是未知的。
- **沙箱是正确性沙箱，不是安全沙箱。** 受限 builtins + AST 白名单 + rlimit +
  内存看门狗挡得住事故和随手的逃逸，挡不住认真的攻击者。macOS 直接忽略
  `RLIMIT_AS`，内存上限靠父进程轮询 RSS。容器/microVM 是 M2。
- **只有 T1/T5 两层 oracle。** 变形性质（T2）、差分测试（T3）、影子执行（T4）是 M1 ——
  而它们恰好是不需要标准答案的那几层。
- **变异得分门槛 80% 是拍的。** 目前语料里正确实现 95%、弱用例 72%，区分度够用，
  但这个数没有在真实数据上标定过。等价变异体也识别不了（01 唯一的存活就是个真等价变异，
  白白算进了分母）。
- **模型能力表是手写的。** `llm.py` 里按模型记了 thinking / effort / temperature 的
  形状差异（Haiku 4.5 用 `budget_tokens` 且不吃 `effort`；Opus 5 反过来，且
  `temperature` 传了会 400）。新模型要手动加一行。
- **差分测试（T3）在 Opus 5 上不能靠调温度。** 该参数已移除，制造独立实现只能靠
  prompt 变体 —— 这会影响 M1 怎么实现"让调用方裁决"。

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
  infer.py          从例子反推 schema（会分辨"记录"和"映射"）
  llm.py            客户端协议 + 模型能力表 + 回放用的脚本客户端
  prompts.py        合成 prompt，需求/例子放在不可信数据区
  synth.py          合成循环
tests/corpus/       11 个语料用例，每个针对一道关卡
examples/           两个 demo 需求，给 `agentjit compile` 用
```

## 从哪读起

1. [design §4 接口设计](docs/design.md#4-接口设计) — 产品面，四个操作，其余都是实现细节
2. [correctness §2 Oracle 分层](docs/correctness.md#2-oracle-分层) — 五层判据，哪些不需要标准答案
3. [correctness §4 变形性质](docs/correctness.md#4-t2--变形性质一条性质抵一万个用例) — 一条性质抵一万个用例
4. [correctness §8 测试集够不够强](docs/correctness.md#8-测试集够不够强) — 跑过了到底能说明什么
5. [design §7.2 Facade RPC](docs/design.md#72-facade-rpc沙箱里不开洞) — 安全模型里最重要的结构决策
6. [design §10 路线图](docs/design.md#10-路线图) — M0/M1 是立项验证
