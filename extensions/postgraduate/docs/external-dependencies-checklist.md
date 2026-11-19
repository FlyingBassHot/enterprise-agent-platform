# 外部依赖清单（2026-11-18）

| 项目 | 当前状态 | 配置/权限 | 连通性验证与替代 |
| --- | --- | --- | --- |
| 平台 Skill/MCP | 仓库、本地 venv、现生产镜像均已有；用户确认后完成升级 | 使用既有迁移，无核心文件修改 | 临时库验收及生产 stdio/API 冒烟通过，详见 deployment-report |
| 业务 LLM | 安装时复用现有启用且支持 tools 的配置；不新增账户 | --model-id；模型 API Key 使用既有配置 | 本次用脚本化 Mock 验证链路，未做真实业务生成质量验收 |
| Brave Search | 已实现适配，未配置/调用付费接口 | 可选注册与额度；TOOL_SECRET_POSTGRAD_BRAVE_API_KEY → BRAVE_API_KEY | 无 Key 返回 unavailable；可用管理员导入资料替代；费用以提供商账户为准 |
| 高德地址编码 | 已实现适配，未配置/调用接口 | 可选开放平台 Key；TOOL_SECRET_POSTGRAD_AMAP_API_KEY → AMAP_API_KEY | 无 Key 返回 unavailable；手工核对官方校区地址，不自动猜校区 |
| 高校公开网页 | 抓取适配已实现，默认白名单为空 | POSTGRAD_PUBLIC_HOSTS 精确列表；公开可访问、robots 允许 | 验收用模拟响应，未证明特定高校站点可达；失败可手工导入 |
| PDF/Word | 本地环境 pypdf 7.1.0、python-docx 1.2.0 已有 | 文件仅限扩展 documents，导出仅 exports；可独立 venv | 文本/Word 测试通过；OCR、PDF 导出、聊天附件自动导入不在 MVP |
| SMTP | 代码已适配，未配置收发账户 | 白名单、授权、平台审批；密码 TOOL_SECRET_POSTGRAD_SMTP_PASSWORD 引用 | 测试使用 mock，不发真实邮件；默认 dry-run；没有附件发送 |
| 企业微信群机器人 | 代码已适配，未配置 webhook | 明确群白名单；TOOL_SECRET_POSTGRAD_WECOM_WEBHOOKS 引用 | 测试使用 mock，不发真实消息；不等于个人微信 API |
| Playwright / Qdrant / OCR | 未安装，非 MVP 依赖 | 如需接入，另行核对官方版本/发布者/权限和资源 | 当前结构化+词项检索可运行，不能声称具有向量检索或浏览器能力 |
| 招生 ML | 六接口无模型回退可用 | P4 需合法数据、时间外评估、校准、版本、离线训练 | 没有训练/加载模型，不返回概率，不安装 sklearn/XGBoost |
| 真实招生/真题数据 | 未导入 | 管理员核验来源、口径、版权后离线导入 | tests 示例不进入业务库；至少五校真实数据验收仍待数据准备 |

已核对官方 API 文档，仅用于实现协议，不代表持有账户或凭据：[Brave Web Search](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)、[高德地理编码](https://lbs.amap.com/api/webservice/guide/api/georegeo)。接口收费和配额以部署时账户合同为准；没有申请账户、安装远程 Skill、启用付费请求或代用户外发。
