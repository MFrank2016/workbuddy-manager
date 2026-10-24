# Search / MCP（`/v1/search`、`/v1/mcp`）

迁移 Todo 28/29 的运维说明。搜索逻辑在 core（Go 网关），manager 只做鉴权、限流与转发；
两边的契约见 `codebuddy2api-contracts.md` §4。

## 1. 鉴权

- 两个端点都要求 `Authorization: Bearer <api_key>`（manager 密钥）。
- 处理顺序（与 `/v1/chat/completions` 的 `_authorize` 同源）：
  1. **全局 IP 规则**（`security.get_security_config()` + `iputil.evaluate`）。命中黑名单
     即 `403 ip_blocked`，**即使密钥缺失/无效也一样**；不写 `request_logs`、不打上游。
  2. 密钥解析：缺失 → `401 missing_api_key`，无效 → `401 invalid_api_key`。
  3. `keysvc.validate_tools`：密钥状态（停用/过期/token 配额/积分额度）+ 密钥级
     `ip_allowlist` / `max_ips`。**跳过 realm 与模型白名单**——search/MCP 没有 model 维度。
- manager 转发时**设置**（而非透传）`X-WB2A-Realm` / `X-WB2A-Profile`；客户端伪造的同名头
  会被覆盖。core 侧只对 search/MCP 校验 realm 头，chat 路径忽略它。
- core 额外校验：`X-WB2A-Profile` 必须在密钥 `api_keys[].profiles` 允许集内，否则
  `400 profile_not_allowed`；realm 推导链为 header profile > key.realm > 单一 profile
  隐含的 realm > `cn`，跨 realm 且无依据时 `400 realm_required`。

## 2. 配额与限流

- **限流**：manager 每下游密钥 **60 次/分钟**（固定 60s 窗口，键 = SHA-256(密钥 id)）；
  core 每上游密钥 **600 次/分钟**（键 = SHA-256(Authorization token)，不信任客户端头）。
  超限返回 `429 rate_limit_exceeded` + `Retry-After: 60`。多实例各自计数（契约明确接受近似）。
- **额度**：search/MCP 不产生模型用量（`request_logs` 记 0 token、credit 为 NULL），
  但每次调用都真实请求上游并占用账号额度。密钥的 token 配额与积分额度照常校验：
  耗尽时 `429 quota_exhausted` / `429 credit_quota_exhausted`。
- **计费**：manager 不把 tools 流量计入 `usage_daily` 的模型用量；core 侧只写
  `reqlog.Event{Model:"web_search"/"mcp", HasCredit:false}` 与 `usage.AddKeyed`（无 token）。

## 3. 日志

- manager：成功转发的请求写一行 `request_logs`（`model=web_search|mcp`，0 token，
  credit NULL）；上游 4xx/5xx 原样透传并把状态码/错误片段记入该行。
- 被限流的 429 **只写入站 IP 日志**（`ip_access_log`），不写 `request_logs`：
  请求没有打到上游，记成一次上游调用会让用量视图出现不存在的请求。
- core：`reqlog` 记录 `Model=web_search|mcp`、`HasCredit=false`、路径与状态；
  core **没有** `request_logs` 表（不要在那里找）。

## 4. 限制与开关

- 两个开关默认关闭：manager `tools_search_enabled` / `tools_mcp_enabled`（settings），
  core `search.enabled` / `mcp.enabled`（config.json，可热更）。关闭时端点回**纯文本 404**，
  与路由未注册不可区分。
- 面板保存是**双写**：先写 manager settings，再写默认上游的 core 配置；core 写入失败时
  回滚 manager 的值并返回 502，避免「manager 放行、core 404」的半开状态。
- **非默认上游分组**：core 的开关在各自实例上，面板只写默认 upstream。存在非默认分组时
  面板禁用开关并提示到该实例自己的面板/配置文件设置。
- MCP 是**无状态**的：不发 `Mcp-Session-Id`、不做服务端主动请求、不开 WebSocket；
  只支持 `initialize` / `tools/list` / `tools/call`，工具只有 `web_search`；
  带 `Origin` 头的请求一律 403（DNS rebinding 防护）。
