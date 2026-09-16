# Tracing 前端（M4，未启动）

| 字段 | 值 |
| --- | --- |
| 状态 | Deferred — 等 [design.md](design.md) 的 M0–M3 跑通后再评估 |
| 来源 | v0.1 设计的幸存部分 |

## 它解决什么

[design.md](design.md) 描述的后端有一个前提：**有人告诉它"编译这个"。**

MVP 里这个人是 agent 自己（靠 prompt 指引）或真人。这不可靠——agent 在埋头干活时，很难同时意识到"我这件事已经重复第 7 遍了，该编译成函数"。**人和 agent 都不擅长发现自己的重复。**

Tracing 前端就是替它们发现，并自动把 `compile_function` 调掉。它不改变后端的任何机制，只是自动生成后端的输入：需求文本 + 例子。

```
观察 agent 执行 → 识别重复子任务 → 自动生成 requirement + examples → compile_function
```

**关键点：轨迹天然就是例子。** [design.md §6.2](design.md#62-例子即规格--本设计的核心主张) 把整个正确性押在"调用方愿意给例子"上，这是后端最大的不确定性。而 tracing 前端的每条轨迹都自带真实的 input/output 对——它把后端最脆的假设变成了副产品。这是做这个前端的**首要理由**，比省掉人工触发重要得多。

## 组件

### Tracer

挂在 agent 的 tool 调用链路上，异步记录结构化事件。挂掉不能影响主流程，开销必须可忽略（采样可配，大结果只存摘要）。

```python
@dataclass
class TraceEvent:
    trace_id: str; seq: int; ts: float
    kind: Literal["tool_call","tool_result","llm_step","task_begin","task_end"]
    tool: str | None
    args: dict                              # 脱敏后
    result_digest: str                      # 大结果只存哈希
    result_preview: Any                     # 截断，供理解语义
    provenance: dict[str, ValueOrigin]      # 见下
    cost: Cost
    effects: EffectClass
```

脱敏是硬要求：轨迹会记录 tool 入参和结果，其中可能有凭据和 PII。已知凭据字段名直接丢弃，值层面跑 secret 检测，Trace Store 加保留期（默认 30 天）和访问控制。

### 子任务边界

| 策略 | 来源 | 可靠性 |
| --- | --- | --- |
| 显式 | agent 声明 `with jit.subtask("拉取月度报表")` | 高 |
| 结构 | plan step / TODO 项 / subagent 调用的天然边界 | 中高 |
| 挖掘 | 从 tool-call 序列挖频繁连续子序列 | 中 |

先做前两种。显式和结构边界自带语义标签，这个标签直接就是 `compile_function` 的 `requirement` 雏形。纯挖掘出来的子序列没有名字，得反过来让模型猜它在干什么，噪声大得多。

### 热点探测

不是数次数，是**成本加权**：

```
hotness(sig) = Σ (tokens_spent + λ · wall_clock)

触发条件：
  hotness > K · estimated_compile_cost     # K ≈ 3，预计回本 3 倍才编
  AND observations >= 3                    # 少于 3 条做不了可靠的反合一
  AND variance < V_max                     # 轨迹差异太大说明这不是一件事
  AND max_effect_class <= IDEMPOTENT_WRITE
```

**宁可少编译，也不要编译出低质量产物。** 一个错误产物的代价（悄悄产出错结果 + 排查成本）远高于一个没编译的热点（就是慢点）。

### 参数判定：provenance 比推断可靠

给定 `read("/data/2026-08/a.csv") → write("/out/2026-08.json")`，哪些是参数？让模型猜会猜对大部分，但错的那部分很难发现。Tracer 直接记录每个值的来源，答案是**读出来的**：

| `ValueOrigin` | 判定 |
| --- | --- |
| `USER_INPUT` 来自用户/上游任务输入 | 参数 |
| `UPSTREAM_OUTPUT` 来自本轨迹前序 tool 输出 | 中间变量，编进代码 |
| `ENV` 来自环境（cwd、当前日期、配置） | 跨轨迹变化 → 参数；否则 → 环境断言 |
| `LITERAL` 模型凭空写的常量 | 常量；跨轨迹变化过 → 升格为参数 |

配合反合一做**双确认**：两者结论一致才自动触发编译，不一致则多收集几条轨迹或转人工。廉价且高收益。

### 轨迹规范化

算签名和做反合一之前要先归一：

1. **值抽象** — 具体值 → 类型化占位符（路径、URL、日期、ID 各成一类）
2. **无关步骤剔除** — 默认剔除失败重试，保留探索性只读调用（它可能承载了实际的控制流判断）。需实测调参
3. **顺序归一** — 无依赖的并列调用排序归一，依赖关系从 provenance 图直接得到
4. **循环折叠** — `read(a) read(b) read(c)` → `for x in [a,b,c]: read(x)`。把展开的循环卷回去，对应 tracing JIT 的 loop detection

### 反合一（anti-unification）

对齐 N 条同签名轨迹，结构相同、取值不同的位置即参数候选。这是确定性算法，不是让模型猜——模型只负责最后一步的代码合成，任务被压得很窄，可靠性高得多。

## 自动生成后端输入

```
轨迹簇 ──┬─→ requirement  ← 语义标签 + 归一化骨架 + 参数表（LLM 润色成自然语言）
         └─→ examples     ← 每条轨迹的 (输入参数, 最终输出) 直接成对
```

留出保留集：N 条轨迹拿 1 条不参与合成，只做验收。这是最基本的防过拟合措施——防止合成器把样本背下来。后端 [design.md §11](design.md#11-风险与开放问题) 里"例子太少不舍得留出"的权衡，在这里不存在：轨迹是持续积累的。

## 开放问题

1. **`variance` 怎么量化才靠谱？** 编辑距离是起点，但"多了一次重试"和"少了一个关键步骤"的距离可能一样，语义权重不同。
2. **拦截式 vs 工具式分发。** 自动匹配并替换 agent 的执行步骤（对 agent 透明、零 prompt 成本，但误匹配风险高、agent 不知道自己被换了），还是仍然让 agent 显式调用？建议对高置信命中（精确签名匹配 + 验证充分 + guard 失败率低）才升级为拦截式——对应 JIT 从保守内联到激进内联的演进。
3. **Tracer 的侵入性。** 需要宿主 agent 暴露 tool 调用钩子。不同框架差异很大，可能需要逐个适配，这是"外挂"定位的主要妥协处。
