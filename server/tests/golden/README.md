# 预迁移 golden（W0 基线）

这些文件是**迁移前的协议行为快照**，由 `server/tests/golden_capture.py` 跑真实代码
路径采集，`server/tests/test_golden_protocols.py` 每次运行都会重新采集并逐字节比对。

| 文件 | 内容 |
|---|---|
| `chat_request_body.json` | `/v1/chat/completions` 实际发往上游的请求体 |
| `responses_request_body.json` | `/v1/responses` 翻译后实际发往上游的请求体（上游只有 chat 协议） |
| `anthropic_request_body.json` | `/v1/messages` 翻译后实际发往上游的请求体 |
| `models_listing.json` | `/v1/models` 响应 JSON（含面板侧补齐的 `modalities` / `supported_reasoning_levels`） |
| `retention_defaults.json` | 侧表保留期默认值（`server/db.py` 的模块常量 + `log_retention_overrides` 默认） |

## 归一化规则

采集器在写盘前统一做以下处理，保证**两次采集逐字节相同**：

1. `json.dumps(..., sort_keys=True, ensure_ascii=False, indent=2)` + 末尾换行 ——
   键序与空白由序列化器决定，不依赖字典插入顺序。
2. 只记录**请求**与 `/v1/models` 清单，**不记录响应体**：响应体里有
   `resp_<uuid>` / `msg_<uuid>` / `created_at=now`，属于天生不确定的值，录进来
   就必须逐个打码，反而弱化了「逐字一致」这个判据。
3. 假上游（`httpx.MockTransport`）的响应是写死的 fixture（固定 `created`
   时间戳、固定模型清单），不取当前时间、不读真实账号目录。
4. 出站 api_key 固定为 `golden-upstream-key`（合成值，非真实凭据），
   `WB2API_BASE` 固定为 `http://upstream.invalid` —— 上游地址与密钥进入
   `_upstream_headers()`，不固定就每次不同。
5. 客户端 key 由 `keysvc.create_key()` 在**临时数据库**里现建，token 随机但
   **不出现在请求体里**（它只决定鉴权通过与否）。

## 重新生成

```bash
cd /root/programming/github/workbuddy-manager
python3 -m server.tests.golden_capture
```

**只在确认行为变更是有意的时候重新生成**，并把 diff 写进提交说明。
