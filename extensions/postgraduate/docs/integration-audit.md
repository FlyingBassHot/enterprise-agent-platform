# P0 接入勘察（2026-11-18）

## 已确认的框架接口

| 能力 | 真实路径及接入方式 |
| --- | --- |
| Agent 配置与发布 | `backend/app/modules/agent/{schemas,service}.py`；`AgentConfig.tools/skill_names` 是显式白名单，`AgentService.publish()` 生成不可变快照 |
| Skill 文件 | `backend/app/modules/skill/loader.py`；YAML front matter，`**/skills/*/SKILL.md`，默认只扫 `app/domain_plugins`；可通过 `SkillService(db, scan_root=...)` 单独同步本扩展，不改变全局扫描根 |
| MCP 注册 | `backend/app/modules/tool/{schemas,mcp_service}.py`；`MCPServerCreate` → `MCPService.create_server/sync`，工具名称 `mcp__<server>__<remote_name>` |
| MCP 传输 | `backend/app/modules/tool/mcp_client.py` 支持 stdio 和 Streamable HTTP；每次调用单独启动 stdio 进程，因此状态必须落盘 |
| SDK | `backend/pyproject.toml` 已锁定 `mcp==2.3.0`，本地已验证 `mcp.server.mcpserver.MCPServer`；v1 FastMCP 导入不可用，不降级平台 SDK |
| 工具执行 | `backend/app/modules/tool/service.py` 统一权限、审批、超时、参数验证与执行日志；MCP 同步默认 low，通知工具须显式提升风险和审批要求 |
| 运行时隔离 | `backend/app/modules/runtime/graph.py` 根据快照工具白名单执行；Skill 指令不能增加权限 |
| 模型 | `backend/app/modules/model/service.py`；复用现有启用且支持 tools 的模型，不修改默认模型 |
| 持久化 | 主库只写现有 Agent/Prompt/Skill/MCP/Tool 实体，业务库放本扩展独立 data 目录；无需 migration |
| Docker | `docker-compose.yml` 的后端只挂载 `/data`；当前镜像不会自动包含根目录 extensions，提供独立 compose override 挂载扩展 |

## 能力缺口与无侵入替代

- 没有自动加载根目录 extensions 的 hook：使用显式管理 CLI；不修改启动入口。
- 没有独立业务调度器：MVP 手动授权发送，定时发送未实现，不宣称已定时推送。
- 没有已确认的搜索/地图/浏览器 MCP：通过单独只读 research MCP 适配可选 API；未配置返回 unavailable，浏览器为可选管理员注册项。
- 没有可校准个体录取模型与真实招生数据：ML 默认 unavailable；测试 fixture 永远不导入实际业务库。
- Tool 管理 API/管理员可单独测试工具；实例白名单约束 Agent 运行时，不能表述为对平台管理员的隔离。
- 知识提案审核和招生写入不绑定普通 Agent，避免把 LLM 提案直接当事实。

## 权限矩阵

| 实例 | 查询 | 写入 | 外发 | 管理员能力 |
| --- | --- | --- | --- | --- |
| university-advisor | 招生/来源/ML/研究工具 | 无 | 无 | 招生证据导入独立启用 |
| exam-coach | 大纲/章节/知识/题目/研究工具 | 节点关系提案、受校验题目与反馈 | 默认 dry-run，白名单、授权、审批 | 提案审核不绑定 |
| 既有 Agent | 不追加任何工具或 Skill | 不变 | 不变 | 不变 |

## P0 验收方法

使用独立临时平台库和真实 stdio ping，验证发现→调用→禁用→卸载；发布两个 Agent 并比较快照工具集合；安装重复执行不新增版本；检查原 Agent 配置保持相同。完整结果记入 `test-report.md`，未执行项目不得标成通过。
