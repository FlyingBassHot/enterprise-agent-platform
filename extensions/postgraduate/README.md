# 考研双智能体扩展

独立新增 `postgrad-university-advisor`（院校推荐）与 `postgrad-exam-coach`（资料与练习），包含 14 个本地 Skill、6 个 stdio MCP、显式注册/卸载 CLI 和隔离验收测试。**没有修改平台核心、公共 Schema、路由、默认 Agent、基础依赖或原 compose 文件。**

业务数据库、文档、导出和限流记录位于本扩展 `data/`（可配置），不混入主库业务表。主库只使用现有 Agent/Prompt/Skill/MCP/Tool 注册实体。

## 当前能力

| 模块 | 已实现 | 条件或限制 |
| --- | --- | --- |
| 院校推荐 | 六个研究/抽取/核验/地理/风险/排序 Skill；五类分数线独立存储与证据查询 | 空库不生成院校数据；真实数据须管理员核验导入；未导入至少五所同专业真实院校，不能宣称 P1 数据验收完成 |
| 研究 MCP | Brave 搜索、公开 HTTPS 文本抓取、高德校区地址编码 | Key/主机白名单为可选配置；缺少时返回 unavailable；搜索摘要不当作已验证来源 |
| ML MCP | 六个稳定接口，严格无模型回退 | 未训练/加载任何模型，不返回录取概率；P4 训练、校准和在线推理未实现 |
| 资料练习 | 大纲与知识点提案/审核、关系/先修查询、按课程隔离检索、题目与作答记录 | MVP 是结构化过滤 + 词项匹配，不是向量或完整 BM25；知识图谱审核由管理员执行 |
| 文档 MCP | 限范围 PDF/DOCX/文本解析，带页码或段落定位；DOCX 导出 | 管理员先导入 `data/documents`；没有聊天二进制附件自动导入、OCR、PDF 导出或网页二进制下载 |
| 通知 MCP | 邮件/企微接口、白名单、授权记录、幂等、限流、状态查询与 dry-run | 默认不外发；没有定时调度；当前不发送附件，只发送正文，导出文件由管理员另行交付 |
| 外部浏览器 | 未安装 | 可另行审核并注册 Playwright MCP，当前实例不声称拥有浏览器能力 |

所有测试数据都在临时目录，**不作为真实招生数据或真题导入业务库**。模型为 Mock 时，只能验证链路，不能代替业务 LLM 生成推荐/练习。

## 运行前检查

依赖仓库当前版后端的 `app.modules.skill`、`app.modules.tool.mcp_service` 和 `mcp==2.3.0`。本 SDK 使用 `MCPServer`，不使用旧 FastMCP。

```bash
PYTHONPATH=backend backend/.venv/bin/python -c 'from app.modules.tool.mcp_service import MCPService'
```

上述检查从项目根目录执行。本机已有后端虚拟环境可复用：没有安装或修改任何基础依赖。若 MCP 独立部署，用自己的 venv 安装 `requirements.txt`，文档服务另用 `requirements-documents.txt`；通过 `--python` 指定解释器。独立 MCP 环境不能用于运行需要平台模块的 `manage.py`。

## 本地后端注册

平台必须已经初始化，存在启用的管理员和支持工具调用的模型。CLI 不创建用户、不建公共表、不更换默认模型。工作目录与后端运行目录一致，避免 SQLite 相对路径指错数据库。

```bash
cd backend
.venv/bin/python ../extensions/postgraduate/manage.py install --actor admin
```

可用 `--model-id <现有模型ID>` 显式选模型；否则复用启用且支持工具的默认模型，再回退非 Mock 模型。JSON 输出含实际模型类型，Mock 不能标成实际业务能力已上线。

重复安装不新建相同 Agent/Prompt/Skill 版本。遇到同名其他资源或已被管理员禁用的服务器会拒绝覆盖。安装中的网络/进程错误可能留下部分注册，修复后重复执行即可；服务层自行提交，因此不承诺跨实体原子安装。

## Docker 接入（涉及现有服务变更，执行前确认）

2026-11-18 升级前实测：旧后端为 `APP_ENV=prod`，镜像中没有 `mcp`、`app.modules.skill` 或 `app.modules.tool.mcp_service`，不能直接接入。用户确认后已完成升级及两个实例发布，当前状态见 [部署记录](docs/deployment-report.md)。

兼容方式是先备份主库与现有镜像，再使用**仓库既有** Dockerfile/迁移升级平台版本，并额外挂载本扩展。此操作会重启后端且执行已有迁移，是单独的部署动作，不是本扩展改内核。未获确认前不运行，不跳过或修改迁移。

```bash
# 从项目根目录，完成备份与升级确认后执行。
docker compose -f docker-compose.yml -f extensions/postgraduate/compose.override.yml up -d --build backend
docker compose exec -T backend /app/.venv/bin/python /app/extensions/postgraduate/manage.py install --actor admin --data-dir /postgraduate-data
```

override 仅挂载扩展代码（只读）和独立 `postgraduate-data` 卷；不改原 compose。以后启动需继续带上两个 `-f`。旧镜像无法接入时应停在兼容性报告，不通过复制/覆盖核心模块绕过。

## 可选外部配置

复制 `config/server-env.example.json` 为未跟踪的 `config/servers.local.json`，按管理员选择填写。密钥只用 `${TOOL_SECRET_...}` 引用，实际值置于后端进程/容器环境，不写仓库或聊天。

```bash
cd backend
.venv/bin/python ../extensions/postgraduate/manage.py install --server-env-file ../extensions/postgraduate/config/servers.local.json
```

`config/.env.example` 说明环境变量；不会自动读取或修改生产环境。通知真实模式、白名单和提供商配置须显式设置，并仍受平台 high 风险审批约束。配置变更引起注册差异时先通过平台管理员 MCP 配置核对和迁移，CLI 不静默覆盖；新安装使用 `--server-env-file`。

## 数据与管理员操作

- 将有权处理的文件放进业务目录 `documents/` 后，Agent 调用 `parse_document(filename=...)`；不能读取任意平台附件路径。
- 招生写入、图谱审核和真题导入只通过管理员离线 CLI：

```bash
backend/.venv/bin/python extensions/postgraduate/admin.py import-admissions /path/to/verified-records.json
backend/.venv/bin/python extensions/postgraduate/admin.py review-knowledge /path/to/decisions.json --reviewer operator
backend/.venv/bin/python extensions/postgraduate/admin.py import-question /path/to/verified-question.json
```

招生文件为 `[{"source": {...}, "program": {...}, "record": {...}}]`，字段详见 MCP 的 `upsert_verified_record` docstring。审核文件为 `[{"proposal_id": "...", "decision": "approve", "note": "..."}]`。导入题目文件采用 `save_verified_question` 工具参数格式；真题/改编题需管理员独立核验来源与版权。CLI 按记录返回结果，招生批次可部分成功，退出码 1 表示至少一条失败。

管理员命令可用 `--data-dir` 指向 Docker 独立卷挂载位置。此 CLI 不把写入或审核工具暴露给普通 Agent。来源证据格式验证不等于联网鉴定真伪；运营方需承担核验步骤。

## 验证与卸载

```bash
backend/.venv/bin/python -m pytest -c extensions/postgraduate/pytest.ini extensions/postgraduate/tests extensions/postgraduate/mcp/tests -q
backend/.venv/bin/python -m ruff check --config backend/pyproject.toml extensions/postgraduate
```

真实 stdio 测试需要允许创建子进程与管道；限制该能力的沙箱需在授权后运行。测试使用临时主库与业务库，不使用现有服务数据库。结果见 [test-report.md](docs/test-report.md)。

```bash
cd backend
.venv/bin/python ../extensions/postgraduate/manage.py uninstall --actor admin
```

卸载前先检查其他 Agent/Workflow 是否引用本扩展：工具、Skill 或工作流引用会阻止卸载；外部 Agent 引用的提示词会保留并报告。业务数据与执行历史保留。卸载注册后再移除本扩展挂载；不删除主库、原 Agent 或默认模型。备份、恢复及权限矩阵见 [operations.md](docs/operations.md) 和 [integration-audit.md](docs/integration-audit.md)。
