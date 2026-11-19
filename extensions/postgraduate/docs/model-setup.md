# OpenCode Go / DeepSeek 接入（2026-11-19）

已添加模型 `opencode-go-deepseek`，模型 ID `d5a44dbe-fc11-429e-8d65-ffc5cf0037cb`，上游实际模型 `deepseek-v4.1-flash`（DeepSeek V4.1 Flash 1017）。用户提供的 Key 通过 stdin 输入并由平台现有加密机制存入 model_providers；没有写入源文件、compose 或此文档。

两个考研实例已从 Mock 切换至该模型并发布 v2；原审计实例仍绑定原 Mock，平台默认模型未改。

当前 OpenCode Go 要求请求携带自有 User-Agent 与 x-opencode-session，现有核心客户端没有自定义请求头接口，因此使用独立内网 sidecar `opencode-go-adapter:8011` 适配。代码位于 model_adapter.py，随 compose.override.yml 启动，不修改平台核心。上游固定为官方 Go API；不开放宿主机端口、不记录 Key。会话标识由消息初始 system/user 内容的哈希生成；上下文被裁剪或初始提示变化时锚点可能变化，并非平台 conversation_id 的精确透传。

适配器关闭上游 thinking 模式，避免平台标准 ChatMessage 不保留 reasoning_content 导致工具续答失败。普通回答、工具调用、工具结果续答、SSE 流式输出均以真实订阅请求验证通过，最终测试返回 OK。

后续启动：

```bash
docker compose -f docker-compose.yml -f extensions/postgraduate/compose.override.yml up -d backend
```

若重新运行扩展 install，应显式选择该模型，避免选回仍保留的默认 Mock：

```bash
docker compose exec -T backend /app/.venv/bin/python /app/extensions/postgraduate/manage.py install --actor admin --data-dir /postgraduate-data --model-id d5a44dbe-fc11-429e-8d65-ffc5cf0037cb
```

搜索/地图 Key、核验招生与题库数据仍需独立配置，接入真实 LLM 不会自动获得这些数据。

官方协议来源：https://opencode.ai/docs/console/go/ 。平台没有自有 BPE/tokenizer；服务端完成实际分词。openai_compatible.py 的流式用量兜底为每个输出片段 max(1,len(content)//4)，属于近似计数，不是模型 tokenizer。
