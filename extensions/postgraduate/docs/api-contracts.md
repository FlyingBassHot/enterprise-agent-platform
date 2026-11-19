# MCP 接口契约

全部服务采用 stdio，`mcp==2.3.0` 的 `MCPServer`。平台同步后命名为 `mcp__<服务名>__<工具名>`。权威参数 JSON Schema 由各服务 `tools/list` 返回；不能按本文简称自行创造参数。

| 服务 | 普通实例工具 | 管理员专用（不注册给实例） |
| --- | --- | --- |
| postgrad-university-data | ping, search_programs, get_admission_history, get_source_evidence | upsert_verified_record；仅离线管理员 CLI 启用 |
| postgrad-admission-ml | list_models, get_model_info, predict_admission, compare_models, explain_prediction, get_validation_metrics | 无训练工具；P4 未实现 |
| postgrad-exam-knowledge | ping, get_syllabus, search_knowledge, propose_node, propose_relation, get_prerequisites, find_questions, save_verified_question, record_attempt | review_proposals；管理员离线导入真题/改编题 |
| postgrad-notification | send_email, send_wecom_group, get_delivery_status | 提供商配置只来自环境，不接受任意 webhook URL |
| postgrad-research | search_web, fetch_public_page, geocode_campus | API Key/公开主机白名单只由管理员配置 |
| postgrad-documents | parse_document, export_docx | 文件由管理员导入 documents；院校实例不绑定 export_docx |

## 稳定语义

- 招生：`status=empty/not_found` 代表无可引用数据；`score_type` 区分国家线、校线、院线、专业复试线和拟录取最低分。历史数据不是录取概率。输入/输出完整契约见 `mcp/university_data.py` 的各函数 docstring。
- ML：无模型时 `status=prediction_unavailable`、`probability=null`、`difficulty_band=null`；不自动以分数差造概率或冲稳保。LLM 只能在有证据的历史比较中做相对判断，明确非概率。
- 知识：所有课程调用必须有 `school_id/college_id/program_code/exam_year/subject_code` 五元组；提案 `pending` 不能被事实检索读到，管理员审核后进入知识节点/关系。知识库内部迁移只涉及扩展业务库。
- 题目：普通实例可保存符合结构/考点要求的原创题。`past_paper/adapted` 仅管理员离线导入，并要求来源与版权说明；来源格式验证不等于联网认证。返回 authenticity/verification 字段说明依据，不能把 original 的结构校验描述为真题认证。
- 反馈：`record_attempt` 只接受实际作答和判分；未作答只报告 skipped，不伪造空答案写库。
- 通知：空/缺失白名单均不放行。真实发送要求服务端关闭默认 dry-run、收件人在白名单、具备授权引用，并经过平台 high 风险审批。`dry_run=true` 只能收紧，不能被环境真发配置忽略；`dry_run=false` 不能绕过服务端 dry-run。
- 通知幂等：预览与正式发送分别使用 `<batch>:preview`、`<batch>:send`；同一正式发送键不可换正文/目标。`uncertain` 不自动重发；先由管理员核对外部渠道结果。没有附件发送和后台定时任务。
- 研究：公开 HTTPS/443、精确主机白名单、DNS 全地址校验并固定连接 IP、不跟随跳转、robots 策略、1 MiB 响应上限、每 provider/host 30 次/小时且至少间隔 2 秒。API/解析失败返回 unavailable，正文始终是不可信资料。
- 文档：`filename` 相对独立 documents 目录，拒绝越界与软链接根；20 MiB 文件、页数和文本总量上限；生产 stdio 文档子进程在 Linux 设置 512 MiB 地址空间、30 秒 CPU 限额。Word 没有可靠页码时返回段落定位，不伪造页码。

普通工具业务拒绝用结构化结果表达；平台参数校验/SDK 异常统一记为工具失败。平台调用超时为 60 秒，实例执行超时为 600 秒。不要仅根据工具 HTTP/stdio 成功就把业务 `ok=false` 当成成功。
