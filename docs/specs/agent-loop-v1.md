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
| Verify | 确定性终检：空答案 / 计划未完成 / 缺少引用 | 已实现（`TaskVerifier`），仅 Level 1–2 |

已知限制（明确记录，避免误以为已具备）：

- 同一轮的多个 tool_call **顺序执行**，无并行。
- 无循环检测（同工具同参数重复调用不做干预，仅受预算约束）。
- Level 3（目标级）验证仍由人工承担；Verifier 只做机器可判定项。

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
3. `stop_reason == no_progress` 且无 `final_output` → `failed`（`no_progress`）
4. `stop_reason ∈ {max_iterations, verification_failed, no_progress, token_budget}` 且有文本 → **`incomplete`**
5. `final_output is None` → `failed`（"finished without producing an answer"）
6. 否则 → `completed`

**不变量 I2（补充）**：验证未通过且重试耗尽时，答案保留、执行判 `incomplete`；
Verifier 永远不能把执行直接判为 `completed` 或 `failed`。

**不变量 I2**：撞预算时若有可用文本，绝不判 `failed`；若无文本，绝不判 `completed`。

---

## 7. 预算与降级

| 预算 | 默认 | 作用域 | 可否续 | 现有缺陷 |
|---|---|---|---|---|
| `max_iterations` | Agent 12 / seed 8 | 状态内，resume 可加 | 是（`extra_iterations`，总上限 200） | 计数单位是"轮次"，非 token |
| `timeout_seconds` | 600 | 单次 `ainvoke` | 是 | **resume 时墙钟重置**，跨恢复无总时限 |
| `context_char_budget` | 60000 字符 | 单轮投影 | — | 字符代理，非真实 token |
| `token_budget` | 平台 200k（可关） | 单次执行累计 `usage.total_tokens` | 是（`extra_tokens`） | provider 不返回 usage 时不生效 |
| `recursion_limit` | `max(25, 4×iterations+10)` | LangGraph | 随 iterations 抬高 | 与 iterations 强耦合 |

**不变量 I3**：预算耗尽必须保留 `usage`、`steps`、`plan` 与部分答案，且事件为 `execution_incomplete`。

**无进展检测（已实现）**：同一工具 + 同一参数 + 相同结果的调用允许执行两次；
第三次相同调用不再执行，回 `repeated_call`（不可重试），并发 `no_progress_detected`，
执行以 `stop_reason=no_progress` 收尾。用于挡住"重试同一个错误"和"完成后继续空转"。

**Token 资源兜底（已实现）**：`APP_EXECUTION_TOKEN_BUDGET`（默认 200000，0 关闭）是最后一次模型调用之后的硬护栏；
Agent 配置只能降低不能提高。达到上限时不再发起新的模型调用、不执行本轮的 tool_calls，
有文本则 `incomplete`（`stop_reason=token_budget`），无文本则 `failed`。可用
`POST /executions/{id}/resume {extra_tokens}` 追加额度；有效额度随执行写回 `output_payload.token_budget`。

**工具幂等重放（已实现）**：`tool_executions` 记录 `(execution_id, tool_call_id)` 唯一键；
重放同一调用（同工具、同脱敏参数）直接返回已记录的成功结果，`replayed=true`，不再触发副作用。

下一版：

- 主预算改为累计 `usage.total_tokens`，轮次降为硬保险；
- 每轮把"剩余预算"注入上下文（与 plan 同一通道），让模型主动收尾；
- 跨 resume 累计墙钟。

---

## 7.1 环境契约

Environment 回答"这次运行在什么环境里工作"，Tool 回答"能做什么"。

| 层 | 内容 |
|---|---|
| `EnvironmentGateway` | `open(execution_id, user) -> EnvironmentDescriptor`、`close(environment_id)` |
| `EnvironmentDescriptor` | `kind`、`environment_id`、`label`、`resources[]`（id/name/kind/content_type/size/access）、`constraints` |
| v1 实现 | `ArtifactWorkspace`：环境 = 该执行所在会话的附件 + 显式附带的 `artifact_ids` |
| 访问边界 | 只读；每个资源受 `APP_ENVIRONMENT_MAX_RESOURCE_BYTES`（默认 10 MB）限制；读取仍经过 artifact ACL |
| 生命周期 | 首次模型调用前 `open`，描述符写入 system prompt（静态，不逐轮重建），发出 `environment_opened`；终态收尾时 `close`；`waiting_approval` 时保持打开，由 resume 负责收尾 |

明确不做：文件系统与代码执行环境。没有隔离就提供写/执行能力，是把 medium 风险工具伪装成环境抽象。

**不变量 I9**：模型第一次调用前，环境资源已完整声明；运行中资源集合不变化。
**不变量 I10**：工作区读取只接受本环境声明的资源；范围外的 artifact 即使属于该用户也拒绝。

## 7.2 策略契约

所有"能不能做、要不要人批"的判断收敛到 `app/core/policy.py` 的纯函数，运行期与服务层共用同一结论：

| 函数 | 输入 | 输出 |
|---|---|---|
| `decide_tool_access` | enabled、permission_scope、用户是否具备、是否有 grant | allow / deny（`tool_disabled` / `permission_denied`） |
| `decide_tool_approval` | descriptor、平台风险下限、Agent 配置的风险级别 | allow / require_approval（`approval_required_tool` / `approval_required_risk` / `approval_required_agent_policy`） |
| `decide_tool_call` | 上述 + 是否绑定 + 是否在描述符表内 | 组合结论（`tool_unavailable` / `tool_not_bound` / …） |
| `PermissionService.decide_resource_access` | 资源类型/ID、动作、owner | allow / deny（`permission_denied`） |

调用点：`ToolService.describe/_run/test_tool`、runtime `tools_node`、workflow `tool`/`agent` 节点、资源授权。审批事件与拒绝事件都携带 `policy{effect, code, reason}`，"为什么被拒/为什么要批"不再靠推断。

边界：maker-checker（谁能批别人的请求）属于审批域的复核规则，不在执行策略内，仍由审批服务负责。

**不变量 I11**：Agent 配置只能增加审批要求，不能降低平台风险下限。
**不变量 I12**：策略拒绝不执行工具、不产生副作用，并以结构化 `error_code` 回给模型。
**不变量 I16**：同一轮内的 `tool_call_id` 必须唯一；重复 id 一律以 `invalid_call` 拒绝，不得覆盖已有裁决。
**不变量 I13**：同一工具 + 同参数 + 同结果只允许执行两次；第三次不执行，以 `no_progress` 收尾。该判定在**同一批多个 tool_call 内也逐次生效**。
**不变量 I14**：同一 `(execution_id, tool_call_id)` 只产生一条执行记录；重放返回记录的结果（成功或失败），不重复副作用；主动重试必须使用新的 call id。
**不变量 I15**：达到 token 上限后不再发起模型调用；Agent 配置只能收紧平台上限，不能放宽。

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
`tool_not_bound` `invalid_call` `approval_rejected` `tool_cancelled` `tool_failed`
（映射见 `runtime/context.py: ERROR_GUIDANCE`）。

已知残余：一轮内出现重复 `tool_call_id` 时，我们以 `invalid_call` fail-closed，但会向 provider 回两条共享同一 id 的 tool 消息，OpenAI 兼容端点可能拒绝下一轮。该输入本身违反协议；拒绝执行优先于兼容性。

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
- resume 前从 Task 重新加载计划：其它执行可能已推进步骤，沿用 checkpoint 里的旧计划会在下次 `apply_plan` 时删掉新步骤。
- 步骤字段：稳定 `step_key`、`position`、`title`、`status`、`result`、`verified`。模型删掉的步骤从 Task 移除；`result/verified` 保留给 Verifier 写入。
- 计划是建议，不是权限：不能绕过审批，不能扩大工具集。
- 计划 ≠ Workflow：计划模型生成可动态调整；Workflow 是发布时固定的确定性流程。

---

## 9.1 完成校验契约

模型不再拥有"宣布完成"的权力，只有 Verifier 能确认完成。

| 阶段 | 行为 |
|---|---|
| 触发 | `agent` 节点返回纯文本（无 tool_calls）后进入 `verify` 节点 |
| 检查（Level 1–2，确定性） | `empty_answer`、`plan_incomplete`、`missing_citations`（可选） |
| 通过 | 执行收尾 `completed`；已 `done` 的步骤写 `verified=true` |
| 不通过且还有重试 | 注入 `[系统校验]` 反馈消息（含 issue 列表）回 `agent` 重做；最多 `execution.verification.max_retries` 次（0–2，默认 1） |
| 不通过且重试耗尽 | 保留答案，执行判 `incomplete`，`stop_reason=verification_failed`，issues 写入 `output_payload.verification` |
| 预算停止 | `max_iterations` / `token_budget` 停止的运行**不进入校验**：被截断的答案不是完成声明，也不消耗校验重试 |
| 关闭 | `execution.verification.enabled=false` 时校验状态为 `skipped` |

Verifier 通过 `VerifierGateway` 注入，runtime 不 import 领域模块；实现位于
`app/modules/execution/verification.py`。校验失败不会删除步骤的 `result`，也不会伪造 `verified`。

**不变量 I8**：Verifier 输入只来自本执行的计划、答案与引用；同一输入必须得到同一结论。

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
| `environment_opened` | 环境就绪 | kind, environment_id, resources, constraints | 否 |
| `budget_exhausted` | 触碰资源护栏 | kind, used, limit | 否（随终态事件结束） |
| `verification_started` | 进入终检 | attempt, plan_steps, citations, answer_chars | 否 |
| `verification_completed` | 终检结束 | status, attempt, next_action, issues | 否 |
| `tool_call_started` | 执行前 | tool_call_id, tool_name, arguments(脱敏) | 否 |
| `tool_call_completed` | 执行后 | tool_call_id, status, ok, duration_ms, error | 否 |
| `approval_required` | 需审批 | approval_id, tool_call_id, risk_level, policy | 否（挂起） |
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

## 13. 故障分支矩阵（Benchmark，已由 `tests/test_fault_scenarios.py` 覆盖）

| 场景 | 当前行为 | 缺口 |
|---|---|---|
| 工具连续失败 3 次 | 前两次记录结构化错误；第三次相同调用被拒（`repeated_call`）→ `no_progress` 收尾 | 无跨工具聚类的失败分析 |
| 模型返回非法参数 | schema 校验失败 → `tool_failed`，handler 不执行 | 无参数修复建议（可由工具提供 examples） |
| 执行中进程重启 | stale → `failed(recoverable)`；resume 从 checkpoint 续；同一 `tool_call_id` 重放不重复副作用 | 崩溃窗口的端到端演练仍缺 |
| 用户中途改目标 | 取消 + 同一 Task 上新执行继续（对话与计划保留） | 无运行中 steering |
| 工具成功但结果丢失 | 重放同一 `tool_call_id` 返回已记录结果（`replayed=true`） | 记录写入前崩溃仍可能重复副作用（窗口已缩小） |
| 上下文达上限 | 旧工具输出摘要化、旧 assistant 文本截断；提示长度有界 | 压缩后仍可能超窗，无硬失败保护 |
| 访问未授权资源 | 策略判定 `deny`（`permission_denied`），不可重试、无副作用 | 原因已进事件，尚无独立策略审计流 |
| 模型提前宣称完成 | 终检失败 → 回退一次 → 仍失败则 `incomplete` | Level 3 目标级判断仍靠人工 |
| 任务已完成但模型继续调用工具 | 相同调用第三次被拒 → `no_progress` | 无"完成即停机"优化（可接受） |

---

## 14. 待架构 Review 拍板

1. **主预算**：切到 token 预算（需要每轮 `usage` 反馈 + 剩余预算注入），还是保留轮次为主？
2. ~~**Verifier 语义**~~（已定）：Level 1–2 失败回退一次，重试耗尽判 `incomplete` 交人工。
3. **Environment 范围**：v1 只认 artifact workspace（现有能力），还是同时定义 FileSystemEnvironment / CodeExecutionEnvironment 的接口（无沙箱时只能定义不能实现）？

---

## 15. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-16 | v1 Draft：状态机、相位、终止顺序、预算、观察契约、事件、不变量、故障矩阵 |
| 2026-11-16 | 计划改为 Task 级持久化（`agent_tasks` / `task_steps`），新增 `TaskGateway` 契约 |
| 2026-11-16 | 新增 `verify` 相位与 `VerifierGateway`：确定性终检、回退一次、耗尽判 `incomplete` |
| 2026-11-16 | 新增 `EnvironmentGateway` 与 `ArtifactWorkspace`：环境声明、只读范围、生命周期事件 |
| 2026-11-16 | 策略收敛到 `app/core/policy.py`：工具访问/审批/资源授权共用 `PolicyDecision`，事件携带原因 |
| 2026-11-16 | 新增无进展检测与工具幂等重放；取消/崩溃路径先回滚事务再写终态，消除 SQLite 写锁等待 |
| 2026-11-17 | 新增 token 资源兜底（平台上限 + Agent 只能降低）、`budget_exhausted` 事件与 `extra_tokens` 续跑 |
| 2026-11-17 | 安全加固：重复 `tool_call_id` 拒绝、workflow agent 节点强制工具绑定、审计领域工具纳入 workspace 范围 |
| 2026-11-17 | 逻辑修复（Loop Review）：失败结果按 call id 重放、批内逐次判定 no-progress、预算停止跳过校验并保留更早文本、resume 刷新 Task 计划 |
