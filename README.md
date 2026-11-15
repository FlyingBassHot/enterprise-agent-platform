# Enterprise Agent Platform

通用企业级 Agent 基座：**Agent 配置化、能力复用、执行可控、私有化可插拔**。

本仓库提供平台基础设施，而非单一业务 Agent。默认使用 SQLite、本地文件、进程内执行器与离线 Mock 模型，可替换为 PostgreSQL、MinIO、Redis Worker 与 vLLM / LiteLLM；当前实现范围为 **Phase 0–2 核心闭环 + Audit 领域验证**。

详细架构与阶段规划见 [`roadmap.md`](./roadmap.md)。

---

## 架构

```mermaid
flowchart LR
  UI[Vue 3 前端] --> API[FastAPI /api/v1]
  API --> CP[Control Plane\nAgent / Prompt / Model / Tool / RBAC]
  API --> RT[Runtime Plane\nLangGraph Agent Loop]
  RT --> CAP[Capability Layer\nModel Gateway / Tool Executor / Artifacts]
  CP --> DB[(SQLite / PostgreSQL)]
  RT --> DB
  CAP --> FS[Local FS / MinIO]
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
| Tool Registry | Native / HTTP 工具、JSON Schema 校验、权限范围、风险等级、超时、脱敏、执行记录 |
| Runtime | LangGraph 状态图、流式输出、Tool Calling、上下文构建、工具绑定校验 |
| Execution | execution_id、状态机、事件序号、SSE 断线重连、取消、失败恢复 |
| Approval | 高风险工具人工审批、拒绝回传、批准后恢复、禁止非管理员自批 |
| Artifacts | 文件上传/下载、类型与大小校验、会话归属校验、工具按 artifact_id 读取 |
| Observability | 执行事件、LLM 调用、Tool 调用、Token 用量、Dashboard 聚合 |
| Audit Pack | Excel/CSV 解析、字段识别、重复付款/大额/短期重复/数据质量、行号定位、报告导出 |

## 技术栈

- **后端**：Python 3.12+、FastAPI、SQLAlchemy 2 async、Pydantic 2、Alembic
- **Runtime**：LangGraph（`AsyncSqliteSaver` Checkpointer、`interrupt` / `Command` 审批恢复）
- **前端**：Vue 3.5、TypeScript、Vite、Element Plus、Pinia、Vue Router
- **默认基础设施**：SQLite、本地文件系统、进程内异步 Runner
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

打开 <http://localhost:8080>，使用 `admin` + 上述密码登录。审计演示 Agent 与样例数据会自动 seed。

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
│   │   ├── tool/               # Tool Registry / Executor / HTTP 适配
│   │   ├── runtime/            # LangGraph 适配层 / Checkpointer
│   │   └── execution/          # 执行 / 会话 / 事件 / 审批 / 附件
│   └── domain_plugins/audit/   # 审计领域包（工具 / Prompt / 样例 / 报告）
├── migrations/                 # Alembic
├── seed/                       # 带标注的审计样例数据
├── scripts/                    # seed / 样例生成
└── tests/                      # 单元 + API + Runtime + 审批 + 审计

frontend/
├── src/api/                    # Axios API 封装
├── src/views/                  # Dashboard / Agent / Prompt / Model / Tool /
│                               # Workspace / Executions / Users / Audit
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
4. 前端展示异常明细、数据质量问题、规则命中统计，并调用 `/audit/report` 生成 Markdown 报告。

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

当前结果：**64 个后端测试通过**，前端类型检查与生产构建通过。测试不依赖真实模型、PostgreSQL、Redis 或 MinIO。

---

## 安全说明

- 生产环境强制 32+ 位 `APP_SECRET_KEY`；首次初始化使用随机管理员密码（写入 0600 文件）或 `APP_BOOTSTRAP_TOKEN`。
- API Key 使用 Fernet 加密落库；Provider 变更仅管理员可操作，URL 变更时清除旧密钥。
- HTTP Tool 仅管理员可配置；敏感头只允许 `${TOOL_SECRET_*}` 环境变量引用。
- 工具参数记录前脱敏；审批展示同样脱敏；执行访问按所有者或资源授权校验。
- 高风险/极高风险工具由平台策略强制审批，Agent 配置无法关闭。
- 模型只能调用 Agent 快照中绑定的工具；工具版本在发布时固定。
- 审计文件解析设置行/列/大小上限，并在工作线程执行。

### 当前限制 / 未实现

- Knowledge Base、Skill Registry、Workflow、MCP、Langfuse 未实现（Phase 3–4）。
- 无登录限流、用户上传配额；HTTP Tool 暂无出站域名/IP allowlist。
- 模型 Provider 配置尚未按 Agent 快照固定；生产建议由管理员统一管理并审计变更。
- 分布式 Worker / Redis 队列未接入；当前为单进程内 Runner。
- 审计报告接口直接调用领域函数（带 60s 超时），不写入 `tool_executions`。

## License

未指定。如需开源发布，请补充 LICENSE。
