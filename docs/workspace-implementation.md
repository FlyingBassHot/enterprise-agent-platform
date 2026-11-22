# Agent 工作区重构：实施与验收记录

> 对应需求：`智能体工作区规划.md`
> 状态：P0 功能已实现并验证；T06 并发压测、T11 文件根向量检索未完成（见第 8 节）。

## 1. 结论

- 配置与使用分离：`/workspaces` 先选 Agent，`/workspaces/:agentId` 固定 Agent 身份。
- 会话、任务、执行、事件、文件访问全部在服务端按 `agent_id` / `root_id` 校验。
- 文件根目录为平台托管逻辑目录，多对多授权；用户权限与 Agent 授权取交集。
- 历史 Agent、会话、执行数据未删除；本次迁移只新增两张表。

## 2. 现状与差距（阶段 A 结论）

已有能力（复用，不重写）：

| 能力 | 位置 |
|---|---|
| Agent 草稿/发布/版本快照 | `backend/app/modules/agent/` |
| 执行、会话、消息、任务、SSE 事件 | `backend/app/modules/execution/` |
| RBAC + 资源 ACL（用户/角色授权） | `backend/app/modules/permission/` |
| 知识库/文档级检索权限 | `backend/app/modules/knowledge/service.py` |
| 审计日志与敏感字段脱敏 | `backend/app/modules/audit_log/` |
| 三栏工作台、执行流组合式函数 | `frontend/src/views/chat/WorkspaceView.vue`、`composables/useExecutionStream.ts` |

本次新增/修改：

| 区域 | 文件 | 目的 |
|---|---|---|
| 数据模型 | `backend/app/modules/workspace/models.py` | `file_roots`、`agent_file_roots` |
| 迁移 | `backend/migrations/versions/e2f3a4b5c6d7_workspace_file_roots.py` | 建表、唯一约束、索引；可回滚 |
| 迁移 | `backend/migrations/versions/f3a4b5c6d7e8_conversation_root.py` | `conversations.root_id`，历史会话保持未归档 |
| 服务 | `backend/app/modules/workspace/service.py` | 可用 Agent、会话/任务/执行作用域、文件根授权、路径安全、审计 |
| 路由 | `backend/app/modules/workspace/router.py` | `/workspaces/*`、`/file-roots/*` |
| Agent | `backend/app/modules/agent/service.py` | `list_usable` / `load_usable_agent`，按发布快照校验权限与角色 |
| 执行 | `backend/app/modules/execution/service.py` | 拒绝无 Agent/Workflow 的执行；会话/执行绑定 `root_id`；跨 Agent/工作区拒绝 |
| 执行仓储 | `backend/app/modules/execution/repository.py` | 任务查询支持 `agent_id`；会话支持 `root_id` |
| 校验错误 | `backend/app/core/exceptions.py` | 序列化 Pydantic `ctx` 中的异常，避免 500 |
| 选择页 | `frontend/src/views/chat/WorkspaceSelectView.vue` | 可用 Agent、搜索、最近使用/发布/名称排序 |
| 沉浸式工作区 | `frontend/src/views/chat/WorkspaceView.vue` | 全屏三栏、无全局导航、根目录/会话/文件预览编排 |
| 工作区侧栏 | `frontend/src/components/WorkspaceRootSidebar.vue` | 根目录分组、会话列表、文件树与面包屑 |
| 文件预览 | `frontend/src/components/WorkspaceFilePreview.vue` | 文本/图片/PDF/二进制预览与下载 |
| 配置中心 | `frontend/src/views/settings/FileRootsView.vue` | 新建根目录、授权/撤销、权限级别 |
| 前端 API | `frontend/src/api/platform.ts`、`frontend/src/types/api.ts` | workspace/file-root 类型与调用 |
| 路由 | `frontend/src/router/index.ts` | 工作区路由移出 MainLayout，`immersive` |
| Agent 列表 | `frontend/src/views/agent/AgentListView.vue` | “进入工作区”快捷入口 |
| 测试 | `backend/tests/test_workspace.py` | 18 个隔离/越权/路径/根目录绑定用例 |

## 3. 数据模型与迁移

```text
file_roots(id, name UNIQUE, description, owner_id, storage_path UNIQUE, timestamps)
agent_file_roots(id, agent_id FK->agents ON DELETE CASCADE,
                 root_id FK->file_roots ON DELETE CASCADE,
                 permission read|write,
                 UNIQUE(agent_id, root_id), timestamps)
conversations.root_id FK->file_roots ON DELETE SET NULL (nullable)
```

- `storage_path` 只由服务端生成（`file_roots/<root_id>`），客户端不可传入。
- 现有会话不新增 `agent_id`（已有）；新增 `root_id` 为空表示“未归档”，历史会话无需回填、不会被删除。
- 不删除、不改写历史会话/执行；新增字段与两张新表可回滚。

升级/回滚：

```bash
cd backend
uv run alembic upgrade head
uv run alembic downgrade -1   # f3a4b5c6d7e8：只删除 conversations.root_id
uv run alembic downgrade -1   # e2f3a4b5c6d7：删除 file_roots / agent_file_roots
```

## 4. 服务端校验点

1. 入口 `require_permission("executions:write")` / `("agents:write")`。
2. `load_usable_agent(agent_id, actor)`：必须发布、存在当前版本，且满足快照中的 `required_permissions` / `allowed_roles`。
3. 会话/消息/任务/执行列表均附加服务端确认的 `agent_id`；跨 Agent 的会话 ID 返回 404（不泄露存在性）。
4. `POST /executions`：
   - 必须提供 `agent_id` 或 `workflow_id`，否则 422；
   - `conversation_id` 的 `agent_id` 与请求不一致时 409，且不写入消息；
   - `task_id` 归属不一致时 409。
5. 文件访问：Agent 授权 ∩ 用户/角色 ACL；`read` 不能写；`write` 需同时具备用户写权限。缺少 Agent 授权或用户 ACL 时统一返回 404，只读写操作返回 403。
6. 路径：拒绝绝对路径、`..`、NUL；`resolve()` 后必须仍在根目录内；符号链接逃逸在列表与下载中被过滤/拒绝。删除使用未解析的目录项，删除符号链接不会删除其指向的目标。
7. 授权撤销：下一次请求立即失效；运行中的任务在下次文件操作时重新校验。
8. 恢复执行（`resume` 与审批后恢复）会重新加载当前已发布 Agent/Workflow，并重新校验状态、角色与所需权限；禁用后不能借恢复接口继续跑。
9. 旧 `POST /conversations` 同样要求 Agent 已发布且当前用户可用；Workflow 会话要求 Workflow 已发布且用户有读权限。
10. 文件根管理（创建/列出/授权/撤销）按 owner 隔离：非超级管理员只能管理自己创建的根目录，其他根目录返回 404；授权变更写审计。
11. SSE：`GET /executions/{id}/stream` 先执行 `get_execution` 归属校验，再按 `execution_id` 订阅，事件不跨执行。

## 5. API

```text
GET    /api/v1/workspaces/agents
GET    /api/v1/workspaces/agents/{agent_id}/conversations
POST   /api/v1/workspaces/agents/{agent_id}/conversations
GET    /api/v1/workspaces/agents/{agent_id}/conversations/{conversation_id}
GET    /api/v1/workspaces/agents/{agent_id}/conversations/{conversation_id}/messages
GET    /api/v1/workspaces/agents/{agent_id}/executions
GET    /api/v1/workspaces/agents/{agent_id}/tasks
GET    /api/v1/workspaces/agents/{agent_id}/file-roots
GET    /api/v1/workspaces/agents/{agent_id}/file-roots/{root_id}/entries?path=
GET    /api/v1/workspaces/agents/{agent_id}/file-roots/{root_id}/content?path=
POST   /api/v1/workspaces/agents/{agent_id}/file-roots/{root_id}/files   (multipart: path, file)
DELETE /api/v1/workspaces/agents/{agent_id}/file-roots/{root_id}/files?path=

GET    /api/v1/file-roots
POST   /api/v1/file-roots
GET    /api/v1/file-roots/{root_id}/permissions
POST   /api/v1/file-roots/{root_id}/permissions
DELETE /api/v1/file-roots/{root_id}/permissions/{agent_id}
```

发送消息复用既有 `POST /api/v1/executions`；请求可带 `root_id`，服务端校验 Agent 对该根目录的授权，并把新会话绑定到该工作区。`conversation_id` 与 `root_id` 不一致时返回 409。

## 6. 前端行为

- `/workspaces` 仍是 Agent 选择页；进入 `/workspaces/:agentId` 后路由位于 MainLayout 之外，**不渲染全局导航栏**，只保留顶部“退出工作区”按钮。
- 沉浸式三栏：
  - 左栏“工作区”：按已授权文件根目录分组，每个根目录下展示该工作区的会话；另有“未归档”组容纳没有根目录的历史会话；根目录被撤销后其会话归入“其他会话”，文件不可浏览。
  - 中栏：当前工作区会话的对话与输入框；新会话会绑定当前根目录。
  - 右栏：默认展示此 Agent 最近 8 个会话；点击左侧文件后，右栏切换为文件内容预览（文本/图片/PDF），关闭预览恢复会话列表。
- 深链接 `/workspaces/:agentId/chat/:sessionId` 会服务端校验归属；不属于该 Agent 时提示并回到基础工作区。
- 每个用户+Agent 在 `localStorage` 记录上次会话；进入基础工作区时恢复（仅 UI 选择，不注入提示词）。
- 文件根只读时只允许浏览/预览；读写根目录才显示写操作（当前侧栏仅浏览与预览）。

## 7. 测试与验收

命令与结果：

```text
cd backend && uv run pytest -q          # 258 passed
cd backend && uv run ruff check app tests  # All checks passed
cd frontend && npm run build            # vue-tsc + vite build passed
```

新增 `backend/tests/test_workspace.py` 覆盖：

- 可用 Agent 只含已发布且满足快照权限/角色的实例；
- 会话列表/读取/消息按路径 `agent_id` 隔离，跨 Agent 返回 404；
- 执行与任务列表按 Agent 隔离；
- 共享根目录多 Agent 可读、只读不能写；
- 撤销授权后立即 404，其他 Agent 不受影响；
- 用户 ACL 与 Agent 授权取交集；
- 上传/下载/删除；`../`、绝对路径、符号链接逃逸被拒绝；删除符号链接保留目标；
- 禁用 Agent 从工作中心消失且新执行/恢复执行被拒绝；
- 无 Agent 的执行请求返回 422，不再 500；
- 跨 Agent 会话不能发起执行且不写入消息；
- 旧 `POST /conversations` 拒绝不存在或已停用的 Agent；
- 文件根目录管理按 owner 隔离，非 owner 不能授权/撤销；
- 会话与执行绑定根目录工作区：新会话落到指定 root，跨工作区继续会话返回 409，未授权 root 返回 404；
- Run 绑定发布版本：v1 执行保持 v1，v2 发布后新执行绑定 v2。

验收矩阵：

| 编号 | 结果 | 说明 |
|---|---|---|
| T01 | 通过 | 浏览器验证选择页先于工作区 |
| T02 | 通过 | 422；不会创建无归属会话 |
| T03 | 通过 | 会话绑定 A，顶部固定 A |
| T04 | 通过 | 列表/任务/会话隔离测试 |
| T05 | 通过 | 跨 Agent 会话 API 404 / 执行 409 |
| T06 | 部分 | SSE 按 execution_id 订阅且鉴权；未做双实例并发压测 |
| T07 | 通过 | 同一根目录授权 A/B，均可读 |
| T08 | 通过 | 未授权根目录列表为空、读取 404（不泄露存在性） |
| T09 | 通过 | `..`、绝对路径、符号链接均拒绝 |
| T10 | 通过 | 撤销 B 后 B 立即 404，A 正常 |
| T11 | 未执行 | 文件根尚未接入向量索引；现有知识检索按知识库/文档 ACL 过滤 |
| T12 | 通过 | 浏览器刷新/深链接保持 Agent 与会话 |
| T13 | 通过 | 无效 Agent 与跨 Agent 会话深链接被拒绝 |
| T14 | 通过 | 禁用后列表隐藏、执行 409、历史数据保留 |
| T15 | 通过 | `test_runs_bind_the_published_agent_version`：v1 Run 保持 v1，v2 发布后新 Run 绑定 v2 |
| T16 | 通过 | 新增表迁移；历史表未改写；临时库已执行 upgrade → downgrade -1 → upgrade |
| T17 | 通过 | 用户无目录权限时即使 Agent 有授权也 404 |
| T18 | 通过 | 跨 Agent 会话在执行入口被拒绝；运行时按 conversation 读取历史 |
| T19 | 通过 | 旧 `POST /conversations` 已强制校验 Agent/Workflow 可用性；旧读取接口仍只返回本人历史 |
| T20 | 通过 | 全量回归测试 |

## 8. 已知限制 / 后续工作

- **T11 未完成**：文件根目录只是文件资源，不是知识库。若需要“对根目录内文档做向量检索并按 root 授权过滤”，需要将文件根接入知识库索引（`documents.root_id`、分块携带 `root_id`、检索时二次校验）。当前知识库检索已有 KB/文档级 ACL，但不可宣称满足 root 级向量隔离。
- T06 建议补双 Agent 并发执行 + 双 SSE 订阅的集成测试。
- 文件列表未分页；超大目录建议增加分页/上限。
- 上传在内存中聚合并一次性写盘（受 `max_upload_mb` 限制）；超大文件建议流式写临时文件后原子替换。
- 旧读取接口（`/conversations`、`/executions` 等）在 Agent 停用后仍允许本人查看历史，以满足数据保留；工作区接口对停用 Agent 返回 404。

## 9. 启动与验证

```bash
make migrate
make seed
make dev            # backend :8000
cd frontend && npm run dev   # :5173
```

登录后：

1. 侧边栏“文件根目录”创建根目录并授权 Agent（只读/读写）。
2. “Agent 工作台”选择实例进入工作区。
3. 右栏文件根目录可浏览/下载；读写权限可上传/删除。
4. 发送消息后右栏显示执行状态、工具调用、审批与最近任务。
5. 刷新页面或直接打开 `/workspaces/:agentId/chat/:sessionId` 验证身份恢复。
