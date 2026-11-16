# Agent Loop Specification v1

状态：Draft（架构 Review 用）
适用范围：`app/modules/runtime`、`app/modules/execution`、`app/core/plan.py`
原则：本文档描述**基座行为契约**；任何领域包不得改变这里的语义，只能通过扩展接口提供能力。

---

## 1. 范围与非目标

本文定义一次 Agent Execution 的：

- 执行状态机与循环相位
- 决策输入与输出协议
- 终止判定与预算语义
- 观察（工具结果）与错误契约
- 计划、上下文、事件的契约
- 可测试的不变量与故障分支

**非目标**：领域工具、领域 Prompt、知识内容、环境实现细节、UI 协议。

---

## 2. 关键设计决策：状态分层

三层必须分开，否则状态数量会爆炸，且无法分别演进：

| 层 | 载体 | 变化频率 | 谁写 |
|---|---|---|---|
| **L-A 执行状态** | `agent_executions.status`（持久化） | 秒级~分钟级 | 只有 `ExecutionService` |
| **L-B 循环相位** | 事件（`message_delta` / `tool_call_started` / ...） | 每轮多次 | runtime 发出，不落状态字段 |
| **L-C 计划状态** | `AgentState.plan` → `output_payload.plan` | 模型主动更新 | 仅 `platform_update_plan` 输出 |

**决策**：不把 `OBSERVING / THINKING / EXECUTING / VERIFYING` 加进数据库 status。
理由是它们属于循环相位，用事件表达即可；写进 status 会产生无意义的迁移与并发写竞争。
`VERIFYING` 在 S4 落地时先以事件 `verification_*` 表达，验证失败不改变 status。

---

## 3. L-A 执行状态机

取值：`pending` `running` `waiting_approval` `completed` `incomplete` `failed` `cancelled`

| 从 | 到 | 触发 | 代码位置 |
|---|---|---|---|
| — | `pending` | 创建执行 | `ExecutionService.create` |
| `pending` | `running` | 调度开始 | `_execute` |
| `running` | `waiting_approval` | 命中审批策略，`interrupt` | `_apply_result_locked` |
| `waiting_approval` | `running` | 审批完成 + resume | `resume` → `_schedule` |
| `running` | `completed` | 自然终止且有终稿 | `_apply_result_locked` |
| `running` | `incomplete` | 预算耗尽且有部分文本 | 同上 |
| `running` | `failed` | 模型/工具/运行时错误 | 同上 / `_ensure_failed` |
| 非终态 | `cancelled` | 用户取消 | `cancel` |
| `incomplete` | `running` | `resume` + `extra_iterations` | `resume` |
| `failed` | `running` | `resume`（重试 / 崩溃恢复） | `resume` |

终态集合：`completed` `incomplete` `failed` `cancelled`（`core/enums.py: TERMINAL_EXECUTION_STATUSES`）。

**不变量 I1（已实现）**：`RuntimeService` 只返回 `RuntimeResult`，永不写数据库 status；
模型输出永远不能把执行置为 `completed`。

---

## 4. 循环相位

一次 `agent_node` 调用 = 一个决策相位；一次 `tools_node` 调用 = 一个行动相位。

```
START → agent(decide) ──有 tool_calls──→ tools(act) → agent …
                    └──无 tool_calls──→ END
```

| 相位 | 内容 | 当前实现 |
|---|---|---|
| Observe | 加载历史 + 知识检索 + 计划注入 + 上下文压缩 | `build_initial_messages`、`compact_messages`、`render_plan_section` |
| Decide | 一次流式模型调用，可能返回文本与多个 tool_calls | `agent_node` |
| Act | 审批解析 → 顺序执行每个 tool_call → 回填观察 | `tools_node` |
| Verify | 检查结果是否支持"完成"声明 | **未实现（S4）** |

已知限制（明确记录，避免误以为已具备）：

- 同一轮的多个 tool_call **顺序执行**，无并行。
- 无循环检测（同工具同参数重复调用不做干预，仅受预算约束）。
- `Verify` 缺位：模型说完成即完成。

---

## 5. 决策协议

模型输出只使用 provider 原生的两种信号：

1. 文本（`message_delta` 累积）
2. `tool_calls[]`（id / name / arguments）

不引入自定义 JSON "action 协议"。理由：多一层解析就多一种失败模式，且主流 provider 的工具调用通道已经稳定；
需要结构化输出时由 Prompt + Verifier 约束，不改协议。

若模型给出未绑定工具或非法参数：

- 未绑定/停用 → tool 消息 `error_code=tool_unavailable`，`retryable=false`
- Schema 非法 → `ToolService` 校验失败 → `error_code=tool_failed`，带 `hint`
- 两种情况都**不**中断循环，模型有机会自纠。

---

## 6. 终止判定（顺序固定）

`_result_from_state` 的判定顺序不可交换：

1. 存在 `__interrupt__` → `waiting_approval`
2. `state.error` → `failed`
3. `stop_reason == "max_iterations"` 且有 `final_output` → **`incomplete`**
4. `final_output is None` → `failed`（"finished without producing an answer"）
5. 否则 → `completed`

**不变量 I2**：撞预算时若有可用文本，绝不判 `failed`；若无文本，绝不判 `completed`。

---

## 7. 预算与降级

| 预算 | 默认 | 作用域 | 可否续 | 现有缺陷 |
|---|---|---|---|---|
| `max_iterations` | Agent 12 / seed 8 | 状态内，resume 可加 | 是（`extra_iterations`，总上限 200） | 计数单位是"轮次"，非 token |
| `timeout_seconds` | 600 | 单次 `ainvoke` | 是 | **resume 时墙钟重置**，跨恢复无总时限 |
| `context_char_budget` | 60000 字符 | 单轮投影 | — | 字符代理，非真实 token |
| `recursion_limit` | `max(25, 4×iterations+10)` | LangGraph | 随 iterations 抬高 | 与 iterations 强耦合 |

**不变量 I3**：预算耗尽必须保留 `usage`、`steps`、`plan` 与部分答案，且事件为 `execution_incomplete`。

下一版（S4 一并做）：

- 主预算改为累计 `usage.total_tokens`，轮次降为硬保险；
- 每轮把"剩余预算"注入上下文（与 plan 同一通道），让模型主动收尾；
- 无进展检测：连续两轮同工具同参数且结果 digest 相同 → `stop_reason="no_progress"`；
- 跨 resume 累计墙钟。

---

## 8. 观察与错误契约

所有 tool 消息的 `content` 是 JSON：

成功：

```json
{"status":"success","ok":true,"output":{...},"error":null}
```

失败：

```json
{"status":"timeout","ok":false,"error_code":"tool_timeout","retryable":true,
 "hint":"…下一步建议…","error":"upstream slow","output":null,"tool":"echo"}
```

`error_code` 取值固定：`tool_timeout` `permission_denied` `tool_unavailable`
`approval_rejected` `tool_cancelled` `tool_failed`（映射见 `runtime/context.py: ERROR_GUIDANCE`）。

**不变量 I4**：每个 `tool_call` 必有配对的 tool 消息，包括被拒绝、未绑定、超时、异常分支。

**不变量 I5**：工具输出是**不可信数据**。其中的指令不得改变系统规则、权限或工具调用策略。

---

## 9. 计划契约

```json
{"plan":[{"id":"1","title":"读取附件","status":"in_progress"}]}
```

- 计划**持久化在 Task 上**（`task_steps` 表），不是 execution 的私有状态；同一 Task 的新执行可以直接续跑。
- 仅 `platform_update_plan` 的返回值能更新计划；runtime 再次 `normalize_plan`（≤20 项，状态枚举固定），经 `TaskGateway` 落库后再注入。
- 每轮作为**独立 system 消息**重注入，位于原 system 之后；本轮投影，不写入会话历史。
- 步骤字段：稳定 `step_key`、`position`、`title`、`status`、`result`、`verified`。模型删掉的步骤从 Task 移除；`result/verified` 保留给 Verifier 写入。
- 计划是建议，不是权限：不能绕过审批，不能扩大工具集。
- 计划 ≠ Workflow：计划模型生成可动态调整；Workflow 是发布时固定的确定性流程。

---

## 10. 上下文契约

投影层级（只影响本轮发给模型的内容）：

| 层 | 内容 | 策略 |
|---|---|---|
| L0 | system prompt、安全规则、Skill 注入 | 永不改写 |
| L1 | 用户目标与输入 | 永不改写 |
| L2 | 计划 | 每轮重建 |
| L3 | 最近 N 条消息 | 原样（`context_keep_recent_messages`） |
| L4 | 较早工具输出 | 确定性摘要：status/error_code/output 键名/digest/原长度 |
| L5 | 较早 assistant 文本 | 仅在前一步仍超预算时截断 |

**不变量 I6**：压缩只作用于投影；`AgentState.messages` 与数据库消息永不改写。

原始工具输出可通过 `tool_executions.result` 与事件流回溯——压缩不等于删除历史。

---

## 11. 事件契约

| 事件 | 时机 | 必要 payload | 终止 |
|---|---|---|---|
| `execution_started` | 开始 | agent_id, agent_version, input | 否 |
| `knowledge_retrieved` | 检索完成 | query, count, citations | 否 |
| `message_delta` | 流式文本 | content | 否 |
| `message_completed` | 一轮模型返回 | content, tool_calls, finish_reason | 否 |
| `plan_updated` | 计划变更 | plan | 否 |
| `context_compacted` | 压缩生效 | chars_saved, budget_chars | 否 |
| `tool_call_started` | 执行前 | tool_call_id, tool_name, arguments(脱敏) | 否 |
| `tool_call_completed` | 执行后 | tool_call_id, status, ok, duration_ms, error | 否 |
| `approval_required` | 需审批 | approval_id, tool_call_id, risk_level | 否（挂起） |
| `approval_resolved` | 审批完成 | approval_id, tool_call_id, approved | 否 |
| `execution_completed` | 完成 | output, usage | 是 |
| `execution_incomplete` | 预算耗尽 | output, usage, stop_reason | 是 |
| `execution_failed` | 失败 | error, agent_id, agent_version | 是 |
| `execution_cancelled` | 取消 | reason, by | 是 |

**不变量 I7**：事件是追加式的，`seq` 单调；终止事件之后不再有该执行的事件。

---

## 12. 与 LangGraph 的边界

当前 LangGraph 是**唯一**执行引擎，因此它是图的执行者，但不是状态权威：

- 权威 A（业务状态）：`agent_executions` + 事件表，由 `ExecutionService` 独占写入。
- 权威 B（循环状态）：LangGraph checkpoint（`thread_id = execution_id`），只服务暂停/续跑。
- 两者通过 `RuntimeResult` + `ExecutionService` 单向衔接，不存在双写同一字段。

**约束**：如果将来引入第二个执行引擎（自研 Direct Loop），必须先在此文档补充
"两个权威的映射表"，并保证 `execution_id → thread_id` 一一对应；在此之前不并行维护两套 loop。

---

## 13. 故障分支矩阵（Benchmark 候选）

| 场景 | 当前行为 | 缺口 |
|---|---|---|
| 工具连续失败 3 次 | 每轮结构化错误 + hint，模型可换策略 | 无"同一错误重复 N 次即止损" |
| 模型返回非法参数 | schema 校验失败 → `tool_failed` | 无参数修复建议（可由工具提供 examples） |
| 执行中进程重启 | 启动时 stale → `failed(recoverable)`；resume 从 checkpoint 续 | 崩溃在工具执行中途的幂等性未验证 |
| 用户中途改目标 | 只能取消后新建执行 | 无 steering（运行中注入用户消息） |
| 工具成功但结果丢失 | 无处理 | 需要工具侧幂等键（`ToolContext` 已有位置） |
| 上下文达上限 | 压缩旧工具输出与旧 assistant 文本 | 压缩后仍可能超窗，无硬失败保护 |
| 访问未授权资源 | `permission_denied`，不可重试 | 无统一 ABAC 事件审计 |
| 任务已完成但模型继续调用工具 | 继续执行直到预算 | 无"完成声明 + Verifier 拒绝"机制（S4） |

---

## 14. 待架构 Review 拍板

1. **主预算**：切到 token 预算（需要每轮 `usage` 反馈 + 剩余预算注入），还是保留轮次为主？
2. **Verifier 语义**：验证失败时"回退一轮"（最多 1 次）还是"直接 `incomplete` 交人工"？
3. **Environment 范围**：v1 只认 artifact workspace（现有能力），还是同时定义 FileSystemEnvironment / CodeExecutionEnvironment 的接口（无沙箱时只能定义不能实现）？

---

## 15. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-16 | v1 Draft：状态机、相位、终止顺序、预算、观察契约、事件、不变量、故障矩阵 |
| 2026-11-16 | 计划改为 Task 级持久化（`agent_tasks` / `task_steps`），新增 `TaskGateway` 契约 |
