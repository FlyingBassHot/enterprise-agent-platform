# Enterprise Agent Platform

通用企业级 Agent 基座：**Agent 配置化、能力复用、执行可控、私有化可插拔**。

本仓库提供平台基础设施，而非单一业务 Agent。默认使用 SQLite、本地文件、进程内执行器与离线 Mock 模型，可替换为 PostgreSQL、MinIO、Redis Worker 与 vLLM / LiteLLM；当前实现范围为 **Phase 0–4 核心闭环 + Phase 6 治理基线 + Audit 领域验证**。

详细架构与阶段规划见 [`roadmap.md`](./roadmap.md)。

---

## 架构

```mermaid
flowchart LR
  UI[Vue 3 前端] --> API[FastAPI /api/v1]
  API --> CP[Control Plane\nAgent / Prompt / Model / Tool / Skill / Knowledge / RBAC]
  API --> RT[Runtime Plane\nAgent Loop / Workflow Executor]
  RT --> CAP[Capability Layer\nModel Gateway / Tool Executor / MCP / RAG / Artifacts]
  CP --> DB[(SQLite / PostgreSQL)]
  RT --> DB
  CAP --> FS[Local FS / MinIO]
  CAP --> VEC[Local Vector Index\nHash Embedding]
  RT --> SSE[SSE 事件流]
  SSE --> UI
```

Agent 执行链路：

```text
用户请求 → 鉴权 → 加载已发布快照 → 构造上下文
        → LangGraph Agent 节点 → 模型流式输出
        → Tool 节点 → 参数校验/权限/风险审批 → 执行工具
        → 循环直至结束 → 保存消息/事件/LLM 调用 → SSE 返回
```

关键设计：

- **管理层与执行层分离**：发布后的 Agent 使用不可变版本快照，运行期不读取草稿。
- **配置即实体**：Agent 的模型、Prompt、工具、执行策略、权限均来自配置。
- **安全不依赖 Prompt**：RBAC、资源授权、工具权限、审批均在服务端强制校验。
- **确定性优先**：审计规则、字段识别、异常定位由确定性代码完成，LLM 负责理解与解释。

---

## 功能

| 模块 | 能力 |
|---|---|
| 认证与权限 | JWT、用户/角色/权限、RBAC、资源级授权、首次初始化令牌 |
| 模型管理 | Provider / Model Config、健康检查、API Key 加密与掩码、Mock + OpenAI 兼容 |
| Prompt | 模板版本、变量 Schema、渲染校验、预览、发布 |
| Agent | CRUD、草稿/发布/停用、版本快照、发布前校验、工具版本固定 |
| Tool Registry | Native / HTTP / MCP 工具、JSON Schema 校验、权限范围、风险等级、超时、脱敏、执行记录；内置 `platform_current_time` / `platform_read_artifact` / `platform_http_request` |
| Knowledge | 知识库/文档权限、PDF/DOCX/XLSX/TXT/MD/CSV 解析、切片、本地 Hash Embedding、混合检索、引用注入 |
| Skill | 文件系统 SKILL.md、版本/依赖、启停、Agent 绑定、运行时指令注入 |
| MCP | stdio / Streamable HTTP Server、工具发现与同步、管理员权限、调用超时 |
| Workflow | 版本化 JSON 定义、Start/LLM/Agent/Tool/Knowledge/Condition/Approval/End、LangGraph 执行、审批/恢复/重试、Agent `workflow`/`hybrid` 模式 |
| Governance | RBAC + ABAC 策略、操作员/审批人角色、审批分离、HTTP 审计日志、登录限流、用户存储配额、SQLite+文件备份/恢复 |
| Runtime | LangGraph 状态图、流式输出、Tool Calling、计划状态、上下文压缩、结构化工具错误、无进展检测、工具幂等重放、完成校验、环境声明、统一策略决策、RAG 引用、Skill 注入、工具绑定校验 |
| Execution | execution_id、状态机（含 `incomplete` 未完成态）、事件序号、SSE 断线重连、取消、失败恢复、预算追加续跑 |
| Task | 任务实体（目标/约束/成功标准）、计划步骤持久化、跨执行续跑（`task_id`） |
| Approval | 高风险工具人工审批、拒绝回传、批准后恢复、禁止非管理员自批 |
| Artifacts | 文件上传/下载、类型与大小校验、会话归属校验、工具按 artifact_id 读取 |
| Observability | 执行事件、LLM 调用、Tool 调用、Token 用量、Dashboard 聚合 |
| Audit Pack | Excel/CSV 解析、字段识别、重复付款/大额/短期重复/数据质量、行号定位、报告导出 |

## 技术栈

- **后端**：Python 3.12+、FastAPI、SQLAlchemy 2 async、Pydantic 2、Alembic
- **Runtime**：LangGraph（`AsyncSqliteSaver` Checkpointer、`interrupt` / `Command` 审批恢复）
- **前端**：Vue 3.5、TypeScript、Vite、Element Plus、Pinia、Vue Router
- **默认基础设施**：SQLite、本地文件系统、进程内异步 Runner、384 维 Hash Embedding + 本地向量扫描
- **可替换**：PostgreSQL、MinIO/S3、Redis、独立 Worker、vLLM / LiteLLM、Langfuse（未接入）

---

## 快速开始

### Docker Compose（推荐）

```bash
export APP_SECRET_KEY=$(openssl rand -hex 32)
docker compose up -d --build

# 首次启动会生成随机管理员密码（0600 权限）
docker compose exec backend cat /data/initial_admin_password.txt
```

打开 <http://localhost:8080>，使用 `admin` + 上述密码登录。审计演示 Agent、审计规则知识库、Audit Skill、审计工作流与样例数据会自动 seed；同时会 seed 内置通用助手 `general-assistant`（绑定平台基础工具），用于验证平台基础能力。

如 8080 端口已被占用，在项目根目录 `.env` 中设置 `FRONTEND_PORT=8081`，再启动并访问 <http://localhost:8081>。

> 生产环境必须设置 `APP_SECRET_KEY`；`APP_ENV=prod` 时不足 32 位或默认值会拒绝启动。

### 本地开发

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run python scripts/seed.py

# 首次 seed 会生成 data/initial_admin_password.txt
cat data/initial_admin_password.txt
uv run uvicorn app.main:app --reload --port 8000
```

另开终端：

```bash
cd frontend
npm ci --legacy-peer-deps
npm run dev
```

前端开发地址 <http://localhost:5173>，通过 Vite 代理访问 `/api/v1`。API 文档：<http://localhost:8000/docs>。

常用 `make` 目标：

```bash
make install     # 安装前后端依赖
make migrate     # Alembic 升级
make seed        # 初始化管理员 + Mock 模型 + Audit 演示
make test        # 后端测试
make build       # 前端生产构建
```

### 内置基础工具

平台自带三个原生工具，启动时自动注册并 `sync` 为可绑定的 Tool 记录，任何 Agent 都可以直接绑定：

| 工具 | 风险 | 说明 |
|---|---|---|
| `platform_current_time` | low | 返回当前时间、日期、星期、时区偏移与时间戳，支持 IANA 时区 |
| `platform_read_artifact` | low | 读取当前用户有权访问的上传附件；文本类附件返回正文摘录，二进制附件只返回元数据 |
| `platform_update_plan` | low | 维护本次任务的计划清单；计划作为 loop 状态每轮重新注入，不写入消息历史 |
| `platform_http_request` | medium | 向管理员白名单内的服务发起 GET/POST；白名单为空时禁止出网，内网/保留地址一律拒绝，不自动跟随重定向 |

出网白名单通过环境变量配置（逗号分隔，精确主机名或以 `.` 开头的后缀）：

```bash
APP_HTTP_TOOL_ALLOWED_HOSTS=api.example.com,.internal.example.com
APP_HTTP_TOOL_MAX_RESPONSE_KB=256   # 单次响应截断上限，默认 256 KB
```

白名单只作用于内置 `platform_http_request`；管理员在「工具管理」中手工登记的 HTTP Tool 仍按各工具自身配置出站。DNS 解析与 HTTP 客户端各解析一次，存在 DNS rebinding 时间窗，因此白名单应只包含自有或可信主机。

### 执行循环

单次执行是一个 ReAct 循环：模型决策 → 工具执行（含审批）→ 观察结果 → 再决策，直到模型不再调用工具。

- **工具固定**：工具描述符在 `run` 入口解析一次并写入执行状态，之后每轮复用，保证模型看到的工具前缀稳定（利于 provider prompt cache），也避免每轮一次数据库查询。
- **计划状态**：`platform_update_plan` 的返回值写入执行状态的 `plan` 字段，**同时持久化到 Task 的 `task_steps`**（`agent_tasks` / `task_steps`），并在每轮模型调用前重新注入为一条独立的系统消息；它不写入会话历史，因此不会被上下文裁剪丢掉，也不会污染多轮对话。同一任务可以用 `POST /executions` 携带 `task_id` 开新执行继续做，计划不会被重置。
- **预算与降级**：`execution.max_iterations` 用尽时，如果已经有模型文本，执行进入 `incomplete`（未完成）而非 `failed`，保留部分答案并发出 `execution_incomplete` 事件；此时可以 `POST /executions/{id}/resume` 带 `extra_iterations` 追加预算继续同一个 checkpoint。没有任何文本才判定失败。
- **上限**：追加后的预算上限为 200 次模型调用；整个执行另受 `execution.timeout_seconds` 约束。
- **上下文预算**：`APP_CONTEXT_CHAR_BUDGET`（默认 60000 字符）是模型上下文的字符替代指标。超出后，较早的工具结果被替换为确定性摘要（保留 status / error_code / output 键名 / 原文 digest / 原始长度），最近 `APP_CONTEXT_KEEP_RECENT_MESSAGES`（默认 6）条消息保持原样；再不够才压缩较早的 assistant 文本。压缩只作用于本轮发给模型的投影，持久化的会话历史不变，并发出 `context_compacted` 事件。
- **工具错误语义**：失败的 tool 消息携带 `error_code`（`tool_timeout` / `permission_denied` / `tool_unavailable` / `approval_rejected` / `tool_failed` / `tool_cancelled`）、`retryable` 与 `hint`，模型据此决定修正参数重试还是如实报告限制，而不是重复盲试。
- **环境声明**：运行开始前打开 `ArtifactWorkspace`，把本次可用的附件（会话附件 + 显式附带）以资源清单写入 system prompt（只读、单资源 `APP_ENVIRONMENT_MAX_RESOURCE_BYTES` 上限），并发出 `environment_opened`。`platform_read_artifact` 在运行中只接受该清单内的资源；工作区之外的 artifact 即使属于同一用户也会被拒绝。执行暂停等待审批时环境保持打开，终态时关闭。
- **Token 资源兜底**：`APP_EXECUTION_TOKEN_BUDGET`（默认 200000，0 关闭）限制单次执行累计 `usage.total_tokens`，Agent 可按 `execution.token_budget` 收紧但不能放宽。达到上限后不再发起模型调用、不执行本轮工具，有文本则 `incomplete`（`stop_reason=token_budget`），无文本则 `failed`；`resume` 可带 `extra_tokens` 追加额度。
- **无进展检测**：同一工具 + 同一参数 + 相同结果的调用最多执行两次；第三次直接拒绝（`repeated_call`，不可重试）并以 `stop_reason=no_progress` 收尾，避免"重试同一个错误"和"完成后继续空转"消耗预算。
- **工具幂等重放**：`tool_executions` 以 `(execution_id, tool_call_id)` 唯一标识一次调用；重放同一调用直接返回已记录的成功结果（`replayed=true`），不重复副作用。
- **策略决策**：工具访问、审批要求、资源授权统一由 `app/core/policy.py` 的纯函数判定（`allow` / `require_approval` / `deny` + `code` + `reason`）。运行时、ToolService、Workflow 节点与 `PermissionService` 共用同一结论；审批与拒绝事件都带 `policy`，Agent 配置只能加严、不能降低平台风险下限。
- **完成校验**：模型输出纯文本后进入 `verify` 相位，由 `TaskVerifier` 做确定性检查（空答案、计划未完成、可选引用检查）。不通过时注入 `[系统校验]` 反馈回退一轮（`execution.verification.max_retries`，默认 1，上限 2）；重试耗尽则保留答案、执行判 `incomplete`，并在 `output_payload.verification` 写入 issues。校验通过会把已 `done` 的步骤标记 `verified`。可用 `execution.verification.enabled=false` 关闭。

### 切换到真实模型

在「模型管理」中新建 `openai_compatible` Provider（如 vLLM / LiteLLM 的 `/v1` 地址），再创建模型配置并绑定到 Agent。密钥加密存储，接口只返回掩码。

### 切换到 PostgreSQL

```bash
export APP_DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/platform'
uv run alembic upgrade head
```

LangGraph Checkpointer 当前默认 SQLite；生产替换为 Postgres Checkpointer 时需同步调整 `app/modules/runtime/checkpoint.py`。

---

## 项目结构

```text
backend/
├── app/
│   ├── api/v1/                 # 路由聚合、Dashboard
│   ├── core/                   # 配置、异常、日志、安全、跨模块契约
│   ├── infrastructure/         # DB、文件存储、模型网关
│   ├── modules/
│   │   ├── permission/         # 用户 / 角色 / RBAC
│   │   ├── model/              # Provider / Model Config
│   │   ├── prompt/             # Prompt 版本
│   │   ├── agent/              # Agent 配置 / 版本 / 发布
│   │   ├── knowledge/          # 知识库 / 解析 / 切片 / 本地向量检索
│   │   ├── skill/              # Skill 扫描 / 版本 / 启停 / 运行时注入
│   │   ├── workflow/           # Workflow 定义 / 版本 / 校验 / LangGraph 执行器
│   │   ├── audit_log/          # 追加式 HTTP 审计日志
│   │   ├── tool/               # Tool Registry / Executor / HTTP / MCP；builtin/ 为平台基础工具
│   │   ├── runtime/            # LangGraph 适配层 / Checkpointer / RAG 上下文
│   │   └── execution/          # 执行 / 会话 / 事件 / 审批 / 附件
│   └── domain_plugins/audit/   # 审计领域包（工具 / Prompt / 样例 / 报告）
├── migrations/                 # Alembic
├── seed/                       # 带标注的审计样例数据
├── scripts/                    # seed / 样例生成
└── tests/                      # 单元 + API + Runtime + 审批 + 审计

frontend/
├── src/api/                    # Axios API 封装
├── src/views/                  # Dashboard / Agent / Knowledge / Skill / Prompt /
│                               # Model / Tool(MCP) / Workspace / Executions / Audit
├── src/components/             # 通用组件与执行时间线
└── src/composables/            # fetch + SSE（Authorization / 断线重连 / 重放）
```

---

## 主要 API

统一前缀 `/api/v1`：

- `POST /auth/login`、`POST /auth/bootstrap`、`GET /auth/me`
- `GET/POST /agents`、`POST /agents/{id}/publish`、`GET /agents/{id}/versions`
- `GET/POST /prompts`、`POST /prompts/{id}/versions`
- `GET/POST /model-providers`、`GET/POST /model-configs`
- `GET/POST /tools`、`POST /tools/{id}/test`
- `GET/POST /mcp-servers`、`POST /mcp-servers/{id}/sync`
- `GET/POST /knowledge-bases`、`POST /knowledge-bases/{id}/documents`、`POST /knowledge-bases/{id}/search`
- `GET/POST /skills`、`POST /skills/sync`、`POST /skills/{id}/enable|disable`
- `GET/POST /workflows`、`POST /workflows/{id}/publish`、`POST /workflows/{id}/execute`
- `GET /audit-logs`、`GET /audit-logs/{id}`（需 `audit:logs:read`）
- `GET/POST /access-policies`、`PATCH/DELETE /access-policies/{id}`（ABAC 策略管理）
- `POST /knowledge-bases/{id}/search`（混合检索 + Metadata Filter）
- `POST /executions`、`GET /executions/{id}/stream`、`POST /executions/{id}/cancel|resume`
- `GET /executions/{id}/tool-calls`、`GET /approvals`、`POST /approvals/{id}/approve|reject`
- `POST /artifacts`、`GET /conversations/{id}/messages`
- `GET /dashboard/stats`
- `GET /audit/sample-file`、`GET /audit/rules`、`POST /audit/report`

SSE 事件带单调 `seq`，支持 `Last-Event-ID` / `?after=` 重放；浏览器断线不会取消执行。

---

## 审计演示

1. 登录后进入「审计演示」，下载示例 Excel。
2. 上传文件并启动分析。
3. Agent 调用 `analyze_transactions`，执行确定性规则，返回带行号的异常清单。
4. Runtime 从 `audit-rules` 知识库检索规则并以 `[K#]` 引用注入上下文；Skill 注入审计方法；前端展示异常明细、数据质量问题、规则命中统计与知识引用，并调用 `/audit/report` 生成 Markdown 报告。
5. 另提供 `audit-review` 演示工作流：Agent 节点 → 条件判断 → 人工复核节点 → 结束，可在「工作流」页面执行并跳转执行详情。

样例：83 笔交易，注入 11 条标注异常（另有 3 条数据质量问题）。当前规则在标注集上 **precision 1.0 / recall 1.0**。测试位于 `backend/tests/test_audit_domain.py` 与 `test_audit_tool_integration.py`。

---

## 测试

```bash
cd backend
uv run pytest -q
uv run ruff check app tests scripts
APP_AUTO_CREATE_TABLES=false uv run alembic check

cd ../frontend
npx vue-tsc --noEmit -p tsconfig.app.json
npm run build
```

当前结果：**230 个后端测试通过**，前端类型检查与生产构建通过。测试不依赖真实模型、PostgreSQL、Redis 或 MinIO。

---

## 安全说明

- 生产环境强制 32+ 位 `APP_SECRET_KEY`；首次初始化使用随机管理员密码（写入 0600 文件）或 `APP_BOOTSTRAP_TOKEN`。
- API Key 使用 Fernet 加密落库；Provider 变更仅管理员可操作，URL 变更时清除旧密钥。
- HTTP Tool 仅管理员可配置；敏感头只允许 `${TOOL_SECRET_*}` 环境变量引用。
- 工具参数记录前脱敏；审批展示同样脱敏；执行访问按所有者或资源授权校验。
- 高风险/极高风险工具由平台策略强制审批，Agent 配置无法关闭。
- 模型只能调用 Agent 快照中绑定的工具；工具版本在发布时固定。
- 审计文件解析设置行/列/大小上限，并在工作线程执行。
- Skill/MCP 管理仅管理员可写；MCP 支持 `${TOOL_SECRET_*}` 引用，stdio 命令由管理员配置。
- 审批采用 maker-checker：执行发起人默认不能自批（超级管理员/`approvals:override` 例外）；`approver` 角色可查看并处理他人审批。
- 所有变更类 API 写入追加式审计日志；登录成功/失败单独记录。请求审计不保存请求体，敏感字段在服务层记录时脱敏。
- 登录、执行创建、文件上传有滑动窗口限流；用户 Artifact 存储默认 200 MB 配额。
- Workflow 中 Agent 节点禁止自动执行高风险工具；Tool/Approval 节点用于显式人工确认。
- 知识检索先做知识库与文档级权限过滤，再做向量/关键词打分；引用内容标记为不可信资料。

### 当前限制 / 未实现

- Langfuse、可视化拖拽 Workflow 编辑器、并行分支/循环、独立 Worker 未实现（后续）。
- Workflow Condition 支持简单比较表达式，不支持复杂表达式语言。
- Workflow Agent 节点仅允许低风险工具；高风险操作应使用 Tool 节点 + Approval 节点。
- 当前 Workflow 为单进程执行，长任务恢复/取消依赖 ExecutionService Checkpointer。
- Knowledge 默认本地 Hash Embedding + SQLite 向量扫描；pgvector/Qdrant 适配器尚未实现。
- MCP 仅支持 stdio / Streamable HTTP，管理员配置；未实现 OAuth 交互式授权。
- 限流为单进程内存实现，多 Worker/多实例需替换 Redis 限流；Knowledge 文档存储未计入用户配额。
- SQLite 为单写者：创建后立即取消执行时，取消写入可能与 worker 启动写入竞争并等待 busy timeout（30s）。生产应使用 PostgreSQL。
- ABAC 已支持按主体角色/部门/租户与资源 ID 匹配的 allow/deny 策略，deny 优先于 owner/grant；复杂条件表达式与属性来源扩展尚未实现。
- 出站白名单仅覆盖内置 `platform_http_request`；管理员登记的 HTTP Tool 与 MCP HTTP 尚无域名/IP allowlist。内置工具存在 DNS rebinding 时间窗。
- 当前为单进程内文档处理与执行 Runner；生产建议拆 Worker。
- Embedding 默认离线 Hash 模型，语义质量有限；生产建议切换到真实 Embedding Provider。
- 模型 Provider 配置尚未按 Agent 快照固定；生产建议由管理员统一管理并审计变更。
- 分布式 Worker / Redis 队列未接入；当前为单进程内 Runner。
- 审计报告接口直接调用领域函数（带 60s 超时），不写入 `tool_executions`。

## 运维：备份与恢复

```bash
# 在 backend/ 下执行；默认读取 APP_DATABASE_URL / APP_STORAGE_DIR
uv run python scripts/backup.py --output ./backups/platform-$(date +%Y%m%d-%H%M%S).tar.gz

# 恢复前停止服务；目标目录非空需 --force（会覆盖同名文件）
uv run python scripts/restore.py --archive ./backups/platform-YYYYmmdd-HHMMSS.tar.gz --data-dir ./data --force
```

备份使用 SQLite online backup API，包含数据库、文件存储和带 SHA256 校验的 manifest；恢复会校验路径穿越/校验和，并使用 `.restore-incomplete` 标记保证失败不产生半恢复状态。Docker 部署可将备份输出到 `/data/backups/`，恢复时先 `docker compose stop backend`。

## License

未指定。如需开源发布，请补充 LICENSE。
