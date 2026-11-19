# 生产升级与注册记录（2026-11-18）

用户确认“升级”后执行。后端现已升级，两个 Agent 已实际注册和发布；前端保持原镜像，访问入口仍是 http://localhost:8081/agents。

## 部署状态

| 项目 | 实际结果 |
| --- | --- |
| 数据库迁移 | f3972fea3839 → c9d1e2f3a4b5；先在一致性备份副本演练成功，再升级实际数据库 |
| 当前后端镜像 | sha256:746b77d8c77c2305af50b4cd64f51b8d91c7916974fa5930502c01fc90570e97 |
| 原镜像保留标签 | enterprise-agent-platform-backend:pre-postgrad-20261118t144155z |
| 备份目录 | extensions/postgraduate/data/upgrade-20261118T144155Z/（Git 忽略，私有权限） |
| 备份内容 | platform-online.db（一致性 SQLite 备份，integrity_check=ok）、platform-data-before-upgrade.tar.gz（后端停止后完整卷快照）、manifest 与校验记录 |
| 演示 seed | 独立 override 设置 APP_SEED_DEMO=false，避免重写现有 Agent/默认模型 |
| 运行状态 | backend healthy；8000 与前端代理 8081 的 /health 均返回 status=ok、database=ok |
| 扩展代码与业务数据 | /app/extensions/postgraduate 只读挂载；业务独立 volume /postgraduate-data |

## 两个实例

| Agent | ID | 状态/版本 | Skills | 工具 |
| --- | --- | --- | --- | --- |
| postgrad-university-advisor | 3bb055c2-5c1c-4088-901c-60134b11e730 | published / v1 | 6 | 14 |
| postgrad-exam-coach | b9a641d6-cd11-400b-8080-a5c2e94b215f | published / v1 | 8 | 16 |

共启用 14 个业务 Skill、6 个 MCP Server 和 27 个 MCP 工具；工具按 Agent 显式绑定，招生写入/图谱审核工具不开放给普通实例，通知发送工具为 high 风险且必须审批。

## 实际验收

- 通过当前运行的 HTTP API 发起两个部署连通性执行，均 completed：coach `2a6b02d7-a5fb-40be-bdd2-7775d345a1ab`，advisor `607e22fc-ea92-4397-bb78-2b634f579af0`。
- 通过实际 ToolService 调用 stdio：招生 ping=ok、知识库 ping=ready、ML list_models=ok（空模型）、搜索在缺 Key 时 unavailable、通知查询未创建的发送键返回 not_found。未发送邮件/企微，未执行付费 API 调用。
- 与升级前备份按原有 ID 比较 agents、agent_versions、model_configs、model_providers、prompts、prompt_versions 的原行哈希，六类记录全部一致。原 `audit-analyst` 仍为 published / v1，发布版本 ID 未变。
- 独立 Herdr omp 只读复核了生产绑定、权限、原实例版本和独立业务卷，未发现旧配置覆盖或越权绑定。
- 本次容器路径适配修复后，扩展测试再次 **66 passed / 9.38 秒**；Ruff 与 Compose 配置验证通过。前次原平台回归为 240 passed，平台源代码没有改动。

## 业务限制与日常启动

初次部署时平台只有 `mock-agent`，上述部署冒烟使用 Mock。2026-11-19 按用户要求添加 OpenCode Go 的 DeepSeek V4.1 Flash 1017，两个考研实例现已切换并发布 v2，真实文本、工具续答及流式检查通过，见 [模型接入记录](model-setup.md)。搜索/地图凭据及核验数据仍需配置；通知默认 dry-run。

以后重建/启动应继续使用独立 override，以保留扩展挂载及关闭演示 seed：

```bash
docker compose -f docker-compose.yml -f extensions/postgraduate/compose.override.yml up -d backend
```

回滚须先停止新后端，保留升级后的当前数据，再使用完整备份恢复与旧镜像配套的主库/文件卷；不能直接把旧镜像连接到已升级数据库。保留现有 platform-data 与独立 postgraduate-data 卷。本次没有执行回滚，也没有删除旧镜像或备份。
