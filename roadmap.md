# Enterprise Agent Platform
## 企业级智能体平台总体开发设计文档

**文档版本：** V1.0  
**项目类型：** 企业级 AI Agent 平台  
**部署方式：** 私有化部署优先  
**核心技术：** Python、FastAPI、LangGraph、Vue 3、本地大模型  
**架构模式：** 模块化单体 + 可扩展能力插件体系

---

# 1. 项目概述

## 1.1 项目背景

随着大语言模型和 Agent 技术的发展，企业开始尝试将 AI 引入实际业务场景，例如审计分析、财务核查、法律咨询、企业知识管理、数据分析等。

但不同企业、不同部门的业务需求存在较大差异：

- 不同企业拥有不同的业务数据和知识体系。
- 不同行业具有不同的业务流程和专业规则。
- 不同岗位需要不同的 Agent 能力。
- 企业对数据安全、权限管理、私有化部署有较高要求。

如果每一个业务场景都单独开发一套 Agent 系统，会造成大量重复开发。

因此，本项目旨在开发一个通用的企业级 Agent 基础平台，通过统一的 Agent Runtime、工具管理、知识管理、模型管理、工作流编排和权限体系，为不同企业、不同业务场景提供可配置、可扩展的 Agent 应用能力。

## 1.2 核心设计目标

平台需要实现以下目标：

1. **Agent 配置化**：通过管理界面创建和管理 Agent，尽可能减少针对单个 Agent 的代码开发。
2. **能力复用**：多个 Agent 可以共享模型、工具、知识库、Skills 和业务服务。
3. **业务解耦**：行业业务逻辑与平台核心基础设施分离。
4. **执行可控**：支持 Agent 状态管理、执行追踪、人工审批、失败恢复。
5. **私有化部署**：支持本地模型、内部数据库、内部文件存储和企业内网环境。
6. **可扩展性**：支持 Native Tool、MCP、HTTP API 等外部能力接入。
7. **安全性**：提供用户身份、角色、资源、工具和数据访问权限控制。
8. **可观测性**：完整记录 Agent 执行过程、模型调用、工具调用及错误信息。

## 1.3 第一版不实现的内容

为了控制项目复杂度，V1 暂不实现：

- 分布式微服务架构。
- 完整的 SaaS 多租户计费系统。
- 面向第三方开发者的公开插件市场。
- 复杂的 Multi-Agent 自主协商系统。
- 自研大模型推理引擎。
- 自研 Agent Runtime。
- 自研通用 Workflow 引擎。
- 完全自主执行且不受权限约束的 Agent。

这些功能需要在核心架构稳定后逐步扩展。

---

# 2. 总体架构设计

## 2.1 架构分层

平台采用六层逻辑架构。

### 第一层：Application Layer（应用交互层）

负责与用户交互。

包括：

- Agent 对话工作台。
- Agent 管理后台。
- 知识库管理界面。
- 工具管理界面。
- Workflow 管理界面。
- 执行任务中心。
- 系统管理界面。

技术：Vue 3 + TypeScript。

### 第二层：Control Plane（平台管理层）

负责定义、配置、管理和发布 Agent。

核心模块：

- Agent Registry。
- Model Manager。
- Prompt Manager。
- Tool Registry。
- Skill Registry。
- Knowledge Manager。
- Workflow Manager。
- Permission Manager。
- Agent Publisher。

管理层负责保存 Agent 的定义和配置，不直接承担长时间运行的 Agent 推理任务。

### 第三层：Runtime Plane（执行运行层）

负责实际执行 Agent。

核心模块：

- Agent Executor。
- Workflow Executor。
- Context Manager。
- State Manager。
- Checkpoint Manager。
- Human-in-the-loop。
- Execution Manager。

核心执行框架采用 LangGraph。

### 第四层：Capability Layer（通用能力层）

提供可以被多个 Agent 复用的能力。

包括：

- LLM Service。
- RAG Service。
- Document Parser。
- Tool Executor。
- MCP Client。
- Skill Loader。
- Data Analysis Service。
- Structured Output Service。

### 第五层：Domain Layer（行业业务层）

提供行业相关的业务能力。

例如：

Audit Domain：
- 审计规则。
- 审计数据模型。
- 审计业务工具。
- 审计 Skill。
- 审计 Workflow。

Finance Domain：
- 财务指标。
- 财务分析工具。
- 财务业务规则。

行业模块不能直接修改平台核心 Runtime，应通过平台定义的接口注册和扩展。

### 第六层：Infrastructure Layer（基础设施层）

包括：

- 本地大模型服务。
- PostgreSQL。
- Redis。
- pgvector / Qdrant。
- MinIO。
- 文件系统。
- 日志与监控设施。

## 2.2 核心架构原则

### 原则一：Agent 是配置实体，不是独立代码项目

平台中的每一个 Agent 都应该具有统一的配置模型。

Agent 通过配置决定：

- 使用哪个模型。
- 使用什么 Prompt。
- 能够访问哪些知识库。
- 可以调用哪些工具。
- 使用哪些 Skills。
- 采用什么执行模式。
- 具有哪些权限。
- 使用什么执行参数。

### 原则二：管理层与执行层分离

Agent 配置的创建、修改和发布属于 Control Plane。

Agent 的实际推理、工具调用和状态流转属于 Runtime Plane。

执行中的 Agent 必须使用明确的配置版本快照，避免运行期间修改配置导致执行行为不一致。

### 原则三：确定性业务优先使用代码和 Workflow

LLM 主要承担：

- 意图理解。
- 任务规划。
- 语义判断。
- 非结构化内容理解。
- 结果解释。
- 自然语言生成。

确定性计算和业务规则优先由程序完成。

### 原则四：安全控制不能依赖 Prompt

用户权限、数据访问权限、工具权限必须由后端服务强制校验。

不能通过在 System Prompt 中告诉模型不能访问某些数据来实现安全隔离。

---

# 3. 技术选型

| 层级 | 技术 | 说明 |
|---|---|---|
| 前端 | Vue 3 | 管理后台和用户工作台 |
| 前端语言 | TypeScript | 类型约束 |
| UI | Element Plus | 管理后台组件 |
| 后端 | FastAPI | API 与业务服务 |
| ORM | SQLAlchemy 2.x | 数据访问 |
| 数据校验 | Pydantic 2.x | Schema 定义 |
| Agent Runtime | LangGraph | 有状态 Agent 执行 |
| Agent 集成 | LangChain | 按需使用模型、工具、检索抽象 |
| 数据库 | PostgreSQL | 平台核心数据 |
| 向量检索 | pgvector | 第一版知识检索 |
| 缓存 | Redis | 缓存、任务协调 |
| 模型服务 | vLLM | 本地大模型推理 |
| 模型网关 | LiteLLM | 可选的统一模型接入 |
| 文件存储 | MinIO | 私有化对象存储 |
| 任务执行 | 独立 Worker | 长时间运行任务 |
| 可观测性 | Langfuse | LLM Trace 和评估 |
| 部署 | Docker Compose | 第一版部署 |

### 3.1 技术选型约束

1. FastAPI 是业务后端的统一入口。
2. LangGraph 只能通过 Runtime 适配层使用，不允许业务模块直接依赖 LangGraph 内部实现。
3. 模型调用统一经过 Model Service。
4. 数据库访问统一经过 Repository / Service。
5. Redis 不作为唯一的长期执行状态存储。
6. 向量数据库不能替代 PostgreSQL 中的业务数据管理。
7. 外部工具必须经过统一 Tool Executor 执行。

---

# 4. 核心模块详细设计

## 4.1 Agent Management

负责 Agent 的创建、配置、管理和发布。

### Agent 配置结构

建议定义统一的 AgentConfig：

- id：Agent 唯一标识。
- name：名称。
- description：描述。
- version：版本。
- status：草稿、已发布、已停用。
- model_config：模型配置。
- prompt_config：Prompt 配置。
- tool_bindings：绑定工具。
- skill_bindings：绑定 Skill。
- knowledge_bindings：绑定知识库。
- execution_config：执行配置。
- permission_config：权限配置。

Agent 配置必须采用 Pydantic Schema 进行校验。

### 执行模式

第一版支持三种模式：

**1. Workflow Mode**

使用确定性工作流完成任务。

适用于标准业务流程。

**2. Agent Mode**

使用 LangGraph Agent 根据用户目标自主选择工具。

适用于开放性任务。

**3. Hybrid Mode**

通过 Workflow 控制总体流程，在指定节点调用 Agent 完成开放性推理。

这是企业业务场景的主要推荐模式。

### Agent 生命周期

Draft → Validating → Published → Disabled → Archived

发布前必须进行：

- 配置完整性检查。
- 模型可用性检查。
- 工具存在性检查。
- 权限配置检查。
- Prompt 检查。
- 基本执行测试。

已发布 Agent 的配置不可直接覆盖，应创建新版本。

## 4.2 Model Management

负责统一接入不同的大语言模型。

定义统一模型接口：

```python
class ModelProvider:
    async def chat(self, messages, config):
        ...

    async def stream(self, messages, config):
        ...

    async def invoke_with_tools(
        self, messages, tools, config
    ):
        ...
```

需要支持：

- 本地模型。
- OpenAI 兼容 API。
- 不同模型供应商。
- 模型参数配置。
- 模型健康检查。
- 超时和错误处理。
- 模型调用记录。

模型服务和 Agent Runtime 必须解耦。

Agent 不应该关心底层使用的是哪一个模型供应商。

注意：模型适配器必须验证目标模型的工具调用能力，不能仅依赖接口兼容性。

## 4.3 Prompt Management

负责 Prompt 的统一管理。

支持：

- Prompt 模板创建。
- Prompt 版本管理。
- Prompt 变量。
- Prompt 测试。
- Prompt 发布。
- Agent 绑定。

Prompt 模板应区分：

- System Prompt。
- Task Prompt。
- Tool Instructions。
- Skill Instructions。

Prompt 变量必须有 Schema 定义，禁止直接将未经处理的用户输入拼接成系统指令。

## 4.4 Tool Registry

这是平台最重要的扩展模块之一。

所有 Agent 可使用的工具必须在统一的 Tool Registry 中注册。

### Tool 类型

第一版支持：

1. Native Tool：平台内部实现的 Python 工具。
2. MCP Tool：通过 MCP Server 提供的工具。
3. HTTP Tool：通过 HTTP API 调用的工具。

### Tool 元数据

```python
class ToolDefinition:
    name: str
    description: str
    version: str

    input_schema: dict
    output_schema: dict

    source_type: str
    permission_scope: list[str]

    risk_level: str
    timeout: int
    enabled: bool
```

### Tool 执行流程

用户请求
→ Agent 选择 Tool
→ 参数 Schema 校验
→ 身份和权限校验
→ 风险等级检查
→ 人工审批（必要时）
→ 执行 Tool
→ 输出标准化
→ 保存执行记录
→ 返回 Agent。

### Tool 安全要求

- 不允许 Agent 任意执行 Python 代码。
- 不允许模型直接访问数据库连接。
- SQL 查询必须经过受限的数据访问服务。
- 高风险操作必须审批。
- 敏感参数必须脱敏记录。
- Tool 执行必须具有超时限制。
- 外部 MCP 服务需要认证和访问控制。

## 4.5 Skill Management

Skill 是对业务任务执行方法、知识、规范和工具使用方式的封装。

Skill 不是单纯的 Tool，也不是 Agent。

建议采用目录结构：

```text
skills/
  audit/
    bank_transaction_audit/
      SKILL.md
      instructions/
      schemas/
      examples/
      resources/
```

Skill 元数据：

```yaml
name: bank_transaction_audit
version: 1.0.0
description: 银行流水异常核查

inputs:
  - transaction_file

outputs:
  - anomaly_records
  - analysis_summary

required_tools:
  - analyze_transactions
  - search_audit_rules

required_permissions:
  - audit.project.read
```

平台需要支持：

- Skill 注册。
- Skill 版本管理。
- Skill 安装和启用。
- Agent 绑定 Skill。
- Skill 依赖检查。
- Skill 与 Tool 的关联。

Skill 内容必须作为不可信业务资料处理，不能让 Skill 文本覆盖平台安全策略。

## 4.6 Knowledge Management

负责企业知识库的创建、文档管理和检索。

### 知识处理流程

文件上传
→ 文件存储
→ 文件解析
→ 文档结构提取
→ Chunk 切分
→ Embedding
→ 向量存储
→ 索引建立
→ 知识库发布。

### 第一版支持文件

- PDF。
- DOCX。
- XLSX。
- TXT。
- Markdown。

### 核心功能

- 多知识库管理。
- 文档上传和删除。
- 文档处理状态。
- 文档版本。
- Chunk 管理。
- 向量检索。
- 全文检索。
- 混合检索。
- Metadata Filter。
- Rerank 扩展接口。
- 文档来源引用。

### 权限设计

每个文档必须具有：

- tenant_id（为未来多租户预留）。
- owner_id。
- knowledge_base_id。
- department_id（可选）。
- access_scope。
- document_version。

检索时必须先执行权限过滤，再将检索结果提供给模型。

结构化业务数据不应该全部进入向量数据库。

## 4.7 Workflow Management

负责业务工作流的定义、管理和执行。

第一版支持：

- Start Node。
- LLM Node。
- Agent Node。
- Tool Node。
- Knowledge Retrieval Node。
- Condition Node。
- Human Approval Node。
- End Node。

建议将 Workflow 定义与执行引擎分离。

Workflow Definition 使用平台自己的 JSON Schema 表示。

LangGraph 负责将其转换为可执行图。

这样未来更换 Workflow Runtime 时，不必修改所有工作流定义。

第一版不必开发复杂的可视化拖拽编辑器。先支持结构化配置和基础可视化，验证执行引擎后再扩展编辑器。

## 4.8 Runtime Management

Runtime 负责创建和执行 Agent 实例。

核心组件：

- Agent Loader。
- Config Snapshot。
- Model Resolver。
- Tool Resolver。
- Skill Resolver。
- Context Manager。
- Graph Builder。
- Execution Manager。
- Checkpoint Manager。

### 执行流程

```text
用户请求
    |
    v
Authentication
    |
    v
Authorization
    |
    v
Agent Loader
    |
    v
加载已发布配置快照
    |
    v
构建运行上下文
    |
    v
Graph Builder
    |
    v
LangGraph Execution
    |
    +----> Model
    |
    +----> Tools
    |
    +----> Knowledge
    |
    +----> Skills
    |
    +----> Approval
    |
    v
保存执行状态
    |
    v
返回执行结果
```

每次执行必须创建唯一 execution_id。

### Execution 状态

- PENDING。
- RUNNING。
- WAITING_APPROVAL。
- COMPLETED。
- FAILED。
- CANCELLED。

### 执行记录

需要保存：

- execution_id。
- agent_id。
- agent_version。
- user_id。
- conversation_id。
- 输入参数。
- 执行状态。
- 开始时间。
- 结束时间。
- 错误信息。
- 输出结果。
- 关联 Trace。

每个 Tool Call 需要单独记录执行事件。

### 状态持久化

使用 LangGraph Checkpointer 管理图执行状态。

使用 PostgreSQL 保存平台执行元数据和业务事件。

对于需要恢复的长任务，必须支持：

- 中断。
- 恢复。
- 超时。
- 取消。
- 审批等待。
- 幂等执行。

不能将用户关闭浏览器视为任务取消。

## 4.9 Permission Management

第一版采用 RBAC 为基础，预留 ABAC。

核心对象：

- User。
- Role。
- Permission。
- Resource。
- Resource Scope。

需要实现：

- 用户认证。
- 角色授权。
- Agent 访问权限。
- Knowledge Base 访问权限。
- Tool 执行权限。
- Project 数据权限。
- 敏感操作审批。

权限校验必须在服务端执行，并且在 Tool 真正执行时再次校验。

对于企业财务和审计数据，建议使用独立的数据访问服务，禁止 Agent 直接持有数据库管理员权限。

## 4.10 Observability

负责记录平台运行情况。

第一版需要支持：

- Agent Execution Trace。
- LLM Call Trace。
- Tool Call Trace。
- Token Usage。
- 执行时间。
- 错误记录。
- 用户反馈。
- 基础评估数据。

每个执行事件都应该关联：

agent_id、agent_version、execution_id、trace_id。

敏感内容不能无条件写入日志，应支持字段脱敏和日志访问权限。

---

# 5. 数据库设计

第一版使用 PostgreSQL。

建议按照业务模块划分数据表。

## 5.1 用户和权限

| 表名 | 作用 |
|---|---|
| users | 用户 |
| roles | 角色 |
| permissions | 权限 |
| user_roles | 用户角色关联 |
| role_permissions | 角色权限关联 |
| resource_permissions | 资源权限 |

## 5.2 Agent 管理

| 表名 | 作用 |
|---|---|
| agents | Agent 基本信息 |
| agent_versions | Agent 版本配置 |
| agent_tool_bindings | Agent 工具关联 |
| agent_skill_bindings | Agent Skill 关联 |
| agent_knowledge_bindings | Agent 知识库关联 |
| prompts | Prompt 模板 |
| prompt_versions | Prompt 版本 |

## 5.3 模型管理

| 表名 | 作用 |
|---|---|
| model_providers | 模型供应商 |
| model_configs | 模型配置 |

密钥不得明文存储，必须使用加密或外部 Secret Manager。

## 5.4 Tool 管理

| 表名 | 作用 |
|---|---|
| tools | 工具定义 |
| tool_versions | 工具版本 |
| tool_permissions | 工具权限 |
| tool_executions | 工具执行记录 |

## 5.5 Skill 管理

| 表名 | 作用 |
|---|---|
| skills | Skill 基本信息 |
| skill_versions | Skill 版本 |
| skill_dependencies | Skill 依赖 |

## 5.6 Knowledge 管理

| 表名 | 作用 |
|---|---|
| knowledge_bases | 知识库 |
| documents | 文档 |
| document_versions | 文档版本 |
| document_chunks | 文档切片 |
| embedding_records | 向量记录或关联信息 |

## 5.7 Runtime 管理

| 表名 | 作用 |
|---|---|
| conversations | 对话 |
| messages | 消息 |
| agent_executions | Agent 执行记录 |
| execution_events | 执行事件 |
| execution_checkpoints | 自定义检查点元数据 |
| approval_requests | 审批请求 |

注意：LangGraph 自身的 Checkpoint 存储应遵循其适配器的数据结构，不要为了统一数据库表而重复实现其内部持久化机制。

## 5.8 Workflow 管理

| 表名 | 作用 |
|---|---|
| workflows | 工作流定义 |
| workflow_versions | 工作流版本 |
| workflow_executions | 工作流执行 |

所有核心数据表应具有必要的创建时间、更新时间和状态字段。

业务资源关联应建立外键和必要索引。

---

# 6. 后端项目结构

采用模块化单体结构。

```text
backend/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── v1/
│   │   │   ├── agents.py
│   │   │   ├── executions.py
│   │   │   ├── conversations.py
│   │   │   ├── models.py
│   │   │   ├── tools.py
│   │   │   ├── skills.py
│   │   │   ├── knowledge.py
│   │   │   ├── workflows.py
│   │   │   └── auth.py
│   │   └── dependencies.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── security.py
│   │   ├── exceptions.py
│   │   └── logging.py
│   │
│   ├── modules/
│   │   ├── agent/
│   │   ├── model/
│   │   ├── prompt/
│   │   ├── tool/
│   │   ├── skill/
│   │   ├── knowledge/
│   │   ├── workflow/
│   │   ├── runtime/
│   │   ├── execution/
│   │   ├── permission/
│   │   └── observability/
│   │
│   ├── infrastructure/
│   │   ├── database/
│   │   ├── redis/
│   │   ├── storage/
│   │   ├── vector_store/
│   │   └── model_gateway/
│   │
│   ├── domain_plugins/
│   │   ├── audit/
│   │   └── finance/
│   │
│   └── workers/
│       ├── execution_worker.py
│       └── document_worker.py
│
├── migrations/
├── tests/
├── scripts/
├── Dockerfile
└── requirements/
```

每个业务模块内部统一采用：

```text
module/
├── models.py
├── schemas.py
├── repository.py
├── service.py
├── router.py
└── exceptions.py
```

模块之间通过 Service 接口交互，禁止跨模块直接访问对方的数据库 ORM 实体。

---

# 7. 前端项目结构

```text
frontend/
├── src/
│   ├── api/
│   ├── assets/
│   ├── components/
│   ├── layouts/
│   ├── router/
│   ├── stores/
│   ├── types/
│   │
│   ├── views/
│   │   ├── dashboard/
│   │   ├── agent/
│   │   ├── chat/
│   │   ├── knowledge/
│   │   ├── tools/
│   │   ├── skills/
│   │   ├── workflows/
│   │   ├── executions/
│   │   ├── models/
│   │   └── settings/
│   │
│   └── utils/
│
├── package.json
└── vite.config.ts
```

## 7.1 主要页面

### Dashboard

展示：

- Agent 数量。
- 执行任务数量。
- 成功率。
- 模型调用量。
- 最近执行记录。
- 系统运行状态。

### Agent Management

支持：

- Agent 列表。
- 创建 Agent。
- 编辑 Agent。
- 模型选择。
- Prompt 配置。
- Tool 绑定。
- Skill 绑定。
- Knowledge 绑定。
- 执行模式选择。
- 发布和版本管理。

### Agent Workspace

用户使用 Agent 的主要界面。

需要支持：

- 流式对话。
- 文件上传。
- 执行状态。
- 工具调用过程展示。
- 审批交互。
- 历史会话。
- 结果导出。

### Knowledge Management

支持知识库创建、文档上传、处理状态、检索测试和文档管理。

### Tool Management

支持工具注册、工具详情、Schema 查看、测试调用和权限配置。

### Execution Center

支持查看：

- 执行任务列表。
- 执行状态。
- Agent 执行详情。
- Tool 调用记录。
- LLM 调用记录。
- 错误信息。
- 审批记录。

### Workflow Management

第一版实现工作流列表、配置和执行测试，后续扩展可视化编辑器。

---

# 8. API 设计

统一 API 前缀：

`/api/v1`

主要接口：

## 8.1 Agent

- GET /agents
- POST /agents
- GET /agents/{id}
- PUT /agents/{id}
- DELETE /agents/{id}
- POST /agents/{id}/publish
- GET /agents/{id}/versions

## 8.2 Execution

- POST /executions
- GET /executions
- GET /executions/{id}
- POST /executions/{id}/cancel
- POST /executions/{id}/resume

## 8.3 Conversation

- POST /conversations
- GET /conversations
- GET /conversations/{id}
- GET /conversations/{id}/messages

## 8.4 Knowledge

- GET /knowledge-bases
- POST /knowledge-bases
- POST /knowledge-bases/{id}/documents
- GET /documents/{id}
- DELETE /documents/{id}
- POST /knowledge-bases/{id}/search

## 8.5 Tools

- GET /tools
- POST /tools
- GET /tools/{id}
- POST /tools/{id}/test

## 8.6 Skills

- GET /skills
- POST /skills
- GET /skills/{id}
- POST /skills/{id}/enable

## 8.7 Workflows

- GET /workflows
- POST /workflows
- POST /workflows/{id}/publish
- POST /workflows/{id}/execute

## 8.8 Approvals

- GET /approvals
- POST /approvals/{id}/approve
- POST /approvals/{id}/reject

对于长时间执行的 Agent，创建任务和获取任务结果应分离。

创建任务返回 execution_id。

前端通过 SSE 获取执行事件：

`GET /executions/{id}/stream`

事件类型至少包含：

- execution_started。
- message_delta。
- tool_call_started。
- tool_call_completed。
- approval_required。
- execution_completed。
- execution_failed。

需要支持断线重连和事件序号，避免前端断线后丢失任务状态。

---

# 9. 第一个业务验证项目：Audit Analysis Agent

本项目不能只停留在基础设施开发，必须有一个实际业务案例验证架构。

选择审计分析作为第一个 Domain Pack。

## 9.1 业务目标

用户上传财务交易 Excel，通过自然语言要求 Agent 分析异常交易。

示例：

“请检查这份交易数据，重点关注重复付款、大额异常交易以及短时间内向同一供应商多次付款的情况。”

## 9.2 执行流程

```text
上传 Excel
    |
    v
Document Parser
    |
    v
数据结构识别
    |
    v
Agent 理解分析需求
    |
    v
加载 Audit Skill
    |
    v
调用交易分析 Tool
    |
    v
Python 确定性分析
    |
    v
查询审计知识库
    |
    v
Agent 解释异常结果
    |
    v
生成结构化分析结果
    |
    v
人工复核
    |
    v
导出分析报告
```

## 9.3 Audit Domain 目录

```text
domain_plugins/audit/
├── manifest.yaml
├── models/
├── tools/
│   ├── transaction_analysis.py
│   └── voucher_matching.py
├── skills/
│   └── bank_transaction_audit/
├── workflows/
│   └── transaction_review.yaml
├── prompts/
└── knowledge/
```

行业模块必须通过平台的注册接口接入，不能绕过 Tool Registry、权限服务和 Runtime。

## 9.4 验收目标

- 能正确解析指定格式的 Excel。
- 能识别字段和数据质量问题。
- 能执行确定性异常检测。
- 能返回可定位的原始交易记录。
- 能引用知识库中的相关规则。
- 能生成结构化异常清单。
- 能展示完整执行过程。
- 能保存执行记录。
- 能进行人工审核。
- 能导出报告初稿。

需要准备带有人工标注结果的测试数据，评估异常检测的准确率、召回率和误报率，而不是只看 LLM 生成结果是否合理。

---

# 10. 开发阶段和里程碑

## Phase 0：项目基础建设

目标：建立工程规范和基础设施。

任务：

- 初始化前后端项目。
- 配置 Docker Compose。
- 配置 PostgreSQL。
- 配置 Redis。
- 配置 MinIO。
- 配置 Alembic。
- 建立统一日志和异常处理。
- 建立测试框架。
- 实现用户认证基础功能。

验收：项目可以通过 Docker Compose 启动，前后端正常通信。

## Phase 1：基础 AI Runtime

目标：实现第一个可以工作的 Agent。

任务：

- Model Provider 抽象。
- 本地模型接入。
- Agent Executor。
- LangGraph 集成。
- 基础 Tool Calling。
- Conversation 管理。
- Execution 管理。
- SSE 流式输出。
- 基础执行日志。

验收：用户可以通过前端与本地模型 Agent 对话，并且 Agent 可以调用注册工具。

## Phase 2：平台管理体系

目标：实现配置化 Agent。

任务：

- Agent CRUD。
- Agent Config Schema。
- Prompt Manager。
- Model Manager。
- Tool Registry。
- Tool Executor。
- Agent Version。
- Agent Publish。

验收：可以在后台创建两个不同 Agent，为它们配置不同模型、Prompt 和工具，而不需要修改 Runtime 代码。

## Phase 3：知识与扩展能力

目标：让 Agent 使用企业知识和外部工具。

任务：

- Knowledge Base。
- Document Parser。
- Embedding。
- Vector Search。
- Metadata Filter。
- MCP Client。
- HTTP Tool。
- Skill Registry。
- Tool Permission。

验收：Agent 可以检索授权知识库，使用 MCP 和 Native Tool 完成业务任务。

## Phase 4：Workflow 和人工介入

目标：实现复杂业务流程。

任务：

- Workflow Definition。
- Workflow Executor。
- Condition Node。
- Tool Node。
- Agent Node。
- Approval Node。
- Checkpoint。
- Resume。
- Retry。
- Cancellation。

验收：完成一个包含 Agent、Tool、条件判断和人工审批的完整业务流程。

## Phase 5：行业模块和审计验证

目标：验证平台的业务复用能力。

任务：

- Audit Domain。
- 审计 Skill。
- 审计 Tool。
- 审计 Workflow。
- Excel 分析。
- 报告生成。
- 执行追踪。
- 业务测试集。

验收：审计 Agent 能完成完整的交易分析闭环，且不需要修改平台核心 Runtime。

## Phase 6：企业级治理和部署

目标：满足真实企业内部使用要求。

任务：

- 完善 RBAC / ABAC。
- 数据隔离。
- 审批体系。
- 审计日志。
- Langfuse 集成。
- 模型健康检查。
- 任务恢复测试。
- 安全测试。
- 私有化部署文档。
- 备份和恢复测试。

验收：能够在独立企业环境中完成部署，用户权限、数据访问和 Agent 执行过程均受到控制。

---

# 11. 非功能性要求

## 11.1 安全

- 所有 API 必须经过身份认证。
- 所有资源访问必须经过权限校验。
- 模型不得直接访问数据库。
- 工具执行必须经过统一执行服务。
- 外部 API 密钥必须安全存储。
- 上传文件必须进行类型和大小校验。
- 防范 Prompt Injection。
- 对模型输出和工具参数进行验证。
- 高风险工具必须具备审批机制。

## 11.2 稳定性

- 模型调用设置超时。
- 工具调用设置超时。
- 对可重试错误采用有限次数重试。
- 长任务支持持久化和恢复。
- 任务取消必须能传递到执行层。
- 工具副作用操作必须支持幂等控制。

## 11.3 可扩展性

- 新增模型不应修改 Agent 核心执行逻辑。
- 新增 Tool 不应修改 Agent 核心执行逻辑。
- 新增 Skill 不应修改 Runtime 核心逻辑。
- 新增行业模块不应修改平台基础模块。
- Workflow 定义不应与底层执行引擎完全绑定。

## 11.4 可测试性

必须建立以下测试：

- 单元测试。
- API 集成测试。
- Agent Runtime 测试。
- Tool 权限测试。
- RAG 检索测试。
- Workflow 测试。
- Checkpoint 恢复测试。
- 行业业务测试。

对于依赖大模型的测试，应支持固定输入、Mock 模型和离线评估数据集，避免每次 CI 都依赖真实模型调用。

---

# 12. Coding Agent 开发约束

以下要求必须严格遵守。

## 12.1 开发原则

1. 严格按照模块化单体架构开发。
2. 不允许将所有业务逻辑写在 API Router 中。
3. 不允许直接在业务代码中创建 LangGraph 实例并散落执行逻辑。
4. 不允许绕过 Tool Registry 调用工具。
5. 不允许在 Agent 配置中保存明文密钥。
6. 不允许把行业业务规则写死在平台核心模块中。
7. 不允许为了扩展性过度抽象尚未验证的功能。
8. 所有数据库结构必须使用 Alembic 管理。
9. 所有 API 必须具有明确的 Pydantic Schema。
10. 所有关键模块必须具有测试。

## 12.2 开发方式

必须采用分阶段开发。

每个阶段执行以下流程：

1. 阅读总体架构文档。
2. 明确本阶段涉及的模块。
3. 输出详细实施计划。
4. 检查现有代码结构。
5. 实现模块。
6. 编写测试。
7. 运行测试。
8. 检查模块间依赖。
9. 更新开发文档。
10. 输出阶段完成报告。

禁止一次性生成整个项目。

## 12.3 每个模块的交付要求

每个模块必须包含：

- 明确的职责说明。
- 数据模型。
- Pydantic Schema。
- Service。
- Repository（适用时）。
- API Router（适用时）。
- 单元测试。
- 必要的接口文档。
- 错误处理。
- 日志记录。

## 12.4 架构变更

如果开发过程中发现原有架构存在问题：

1. 先说明问题。
2. 分析影响范围。
3. 给出修改方案。
4. 说明对现有模块的影响。
5. 更新架构文档。
6. 再执行修改。

不得未经说明随意改变核心架构。

---

# 13. 项目最终验收标准

V1.0 只有在以下条件全部满足时，才算完成：

**平台基础能力**

- 可以配置和管理多个 Agent。
- 可以接入本地大模型。
- 可以统一管理模型和 Prompt。
- 可以注册和管理工具。
- 可以管理 Skills。
- 可以创建和管理知识库。
- 可以执行 Workflow。
- 可以管理 Agent 版本。

**执行能力**

- 支持流式输出。
- 支持 Tool Calling。
- 支持执行追踪。
- 支持人工审批。
- 支持任务恢复。
- 支持任务取消。
- 支持执行错误处理。

**企业治理**

- 用户身份认证。
- 角色权限管理。
- 知识库访问控制。
- 工具执行权限。
- 执行审计记录。
- 敏感信息保护。

**业务验证**

- 至少有一个完整的行业 Domain Pack。
- 至少有两个配置不同的 Agent。
- 两个 Agent 能够复用相同的平台基础能力。
- 新增业务 Agent 不需要修改平台核心 Runtime。
- 可以完成一个真实的业务闭环。

---

# 14. 最终架构目标

平台最终形成以下结构：

```text
Enterprise Agent Platform
│
├── Application Layer
│   ├── Agent Workspace
│   ├── Agent Management
│   ├── Knowledge Management
│   ├── Tool Management
│   └── Workflow Management
│
├── Control Plane
│   ├── Agent Registry
│   ├── Model Manager
│   ├── Prompt Manager
│   ├── Tool Registry
│   ├── Skill Registry
│   └── Permission Manager
│
├── Runtime Plane
│   ├── Agent Executor
│   ├── Workflow Executor
│   ├── State Manager
│   ├── Checkpoint Manager
│   └── Human Review
│
├── Capability Layer
│   ├── LLM Service
│   ├── RAG Service
│   ├── Document Service
│   ├── Tool Executor
│   ├── MCP Integration
│   └── Data Analysis
│
├── Domain Packs
│   ├── Audit
│   ├── Finance
│   └── Legal
│
└── Infrastructure
    ├── Local LLM
    ├── PostgreSQL
    ├── Redis
    ├── Vector Store
    ├── MinIO
    └── Observability
```

**最终设计理念：**

平台负责提供通用基础设施和运行机制，行业模块负责提供专业知识与业务能力，Agent 负责根据用户目标组合和调用这些能力。

通过这种方式，实现从单一 Agent 开发到企业级 Agent 平台建设的转变。

本项目的核心价值不在于开发一个拥有最多功能的 Agent，而在于建立一套可以持续复用、组合、扩展和治理 Agent 能力的基础平台。