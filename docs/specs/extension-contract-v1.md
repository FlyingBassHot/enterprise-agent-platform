# Extension Contract Specification v1

状态：Draft
适用范围：Domain Pack、Native Tool、Verifier、Environment、Model Adapter、Skill、Knowledge
原则：**基座不认识领域；领域不得改变 Loop 语义。**

---

## 1. 两类扩展，不要混为一谈

| 类别 | 载体 | 谁注册 | 现有例子 |
|---|---|---|---|
| **代码扩展** | Python 模块 + 注册函数 | 平台启动或领域包 `register()` | Native Tool、Verifier、Environment、Model Provider |
| **数据扩展** | 数据库实体 + 发布快照 | 管理 API / seed 脚本 | Agent 配置、Prompt、Skill 文件、Knowledge 文档、Model Config |

代码扩展决定"平台能做什么"；数据扩展决定"某个 Agent 用什么"。
一个能力通常两者都有：Tool = 代码注册的 descriptor/handler + 数据库行（启停、权限、版本）；Skill = 文件系统 SKILL.md + 数据库绑定行。

**规则 E1**：代码扩展只能由部署方安装；任何 API 都不能注册 handler。
**规则 E2**：数据扩展可以随时变更，但只在发布时进入不可变快照。

---

## 2. 已实现的扩展点

### 2.1 Native Tool

```python
async def handler(arguments: dict[str, Any], ctx: ToolContext) -> Any: ...
```

- 注册：`NativeToolRegistry.register(ToolDescriptor, handler)`；启动时 `sync_native()` 落库并生成版本。
- 契约：`input_schema` 必须是 object schema；`risk_level` ∈ low/medium/high/critical；`timeout_seconds` ≥ 1；handler 必须是 async。
- 失败：抛 `ToolError` 子类 → 记录为 failed + 结构化错误回给模型；不得抛出未捕获异常以外的控制流。
- 权限：`permission_scope` 由平台策略判定；handler 不得自行决定用户权限。
- 领域包示例：`app/domain_plugins/audit`（`register()` 返回 descriptors）。

### 2.2 Verifier

```python
async def verify(*, execution_id, snapshot, plan, output_text, citations) -> VerificationOutcome
```

- 输出：`passed` / `failed` + `issues[{code,message,step_ids}]` + `next_action`。
- 约束：确定性；同一输入必须同一结论；不得调用模型；不得修改除 `task_steps.verified` 之外的状态。
- 定位：Level 1–2（执行/结果）；Level 3（目标级）留给人工。
- 现有实现：`TaskVerifier`（空答案、计划未完成、可选引用）。

### 2.3 Environment

```python
async def open(*, execution_id, user) -> EnvironmentDescriptor
async def close(environment_id) -> None
```

- 描述符必须列出本环境声明的资源；运行中资源集合不变。
- 读写边界由实现强制（ACL + 范围 + 单资源上限）；没有隔离能力时不得提供写/执行。
- 生命周期：首次模型调用前 `open`，终态 `close`，`waiting_approval` 保持打开。
- 现有实现：`ArtifactWorkspace`（会话附件 + 显式附带，只读）。

---

## 3. 预留扩展点（有契约，无实现）

| 类型 | 契约要求 | 约束 |
|---|---|---|
| Skill | `SKILL.md` 元数据 + 指令正文 | 仅作为不可信提示注入；不得可执行 |
| Knowledge | 检索 gateway（search / validate_bindings） | 结果标注为不可信资料，必须引用 |
| Model Adapter | `chat` / `stream_chat` / `health` / `resolve` | 只做协议转换；不得读取业务状态 |
| Context Provider | 向 system 上下文提供领域片段 | 只读、可截断、不得覆盖安全规则 |

**规则 E3**：新增扩展点必须先有第二个真实实现再进基座；只有一个实现的抽象不进入核心。

---

## 4. Domain Pack 形式

```
app/domain_plugins/<name>/
├── __init__.py        # register(registry) -> list[ToolDescriptor]
├── manifest.yaml      # 声明 tools / prompts / skills / knowledge / verifier / requirements
├── tools/             # handler + descriptor
├── knowledge/         # 默认文档
├── prompts/           # 领域提示词
├── skills/            # SKILL.md
└── seed.py            # 幂等演示 Agent（可选）
```

- 兼容性：`manifest.api_version` 与基座比对；不匹配拒绝注册。
- 依赖：`requirements.runtime` 为版本下限；不得反向依赖其它领域包。
- **规则 E4**：领域包不得 import `app.modules.runtime`、`app.modules.execution` 的内部实现；只能通过 `app.core.contracts` 与 `app.core.policy`。
- **规则 E5**：领域包注册失败不得阻止基座启动，但必须在启动日志与 `/health` 中暴露。

---

## 5. 失败语义

| 扩展 | 失败时 |
|---|---|
| Tool | 结构化错误回模型；不重试同一调用（由 no-progress 保护） |
| Verifier | 视为 `failed`（fail closed），不放过完成声明 |
| Environment | 视为空环境并记录错误；不得静默扩大范围 |
| Knowledge | 视为无检索结果（可选能力），但必须记录 |
| Model Adapter | 执行 `failed`，保留已累计 usage |

**规则 E6**：安全相关扩展 fail closed；增益类扩展可降级，但必须可观测。

---

## 6. 一致性测试

每个扩展点必须有一组契约测试，放在 `backend/tests/`：

| 扩展 | 最小测试 |
|---|---|
| Tool | schema 校验、权限拒绝、超时、幂等重放、handler 未执行断言 |
| Verifier | 通过/失败矩阵、重试上限、verified 落库 |
| Environment | 范围外拒绝、跨用户拒绝、读上限、prompt 注入、暂停语义 |
| Domain Pack | 注册幂等、manifest 校验、基座未改动的端到端执行 |

现有测试映射：`test_builtin_tools.py`、`test_verification.py`、`test_environments.py`、`test_audit_tool_integration.py`、`test_fault_scenarios.py`。

---

## 7. 未决

1. Extension Registry 的统一 manifest 扫描（当前领域包手工 `register()`）。
2. `api_version` 的兼容窗口与弃用策略。
3. 领域包级别的资源配额（工具执行次数、token）。
4. Verifier 的领域组合（多个 verifier 的顺序与短路语义）。

---

## 8. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-17 | v1 Draft：两类扩展、三个已实现契约、规则 E1–E6、一致性测试映射 |
