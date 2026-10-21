# codebuddy2api 迁移：跨仓契约

本文是 `workbuddy2api` → `codebuddy2api` 迁移的**跨仓契约**，从工作计划的
`## Execution strategy` → `### Cross-repo contracts` 逐字落地。后续实现任务只引用本文的章节，
不再各自重新定义协议。

**这是一份前向契约，不是现状说明。** 文中描述的行为在当前两个仓库里**尚未实现**；
每一项都由后续 todo 实现，且都通过 feature flag 默认关闭。读本文时请把每一节都当成"实现目标"，
而不是"已有能力"。

两个仓库：**manager** = `workbuddy-manager`，**core** = `workbuddy2api`。

---

**1. Profile → endpoint（P2）**

| profile | chat base | billing base | web base | Origin/Referer | UA platform | 典型 domain |
|---|---|---|---|---|---|---|
| `cn-cli` | `https://copilot.tencent.com` | `https://www.codebuddy.cn` | `https://www.workbuddy.cn` | `https://www.codebuddy.cn` | `WorkBuddy` | `copilot.tencent.com` |
| `cn-work` | `https://www.workbuddy.cn` | `https://www.workbuddy.cn` | `https://www.workbuddy.cn` | `https://www.workbuddy.cn` | `WorkBuddy` | `www.workbuddy.cn` |
| `intl-cli` | `https://www.codebuddy.ai` | `https://www.codebuddy.ai` | `https://www.codebuddy.ai` | `https://www.codebuddy.ai` | `WorkBuddy AI` | `www.codebuddy.ai` |
| `intl-work` | `https://www.workbuddy.ai` | `https://www.workbuddy.ai` | `https://www.workbuddy.ai` | `https://www.workbuddy.ai` | `WorkBuddy AI` | `www.workbuddy.ai` |

- **Precedence:** explicit `profile` > persisted derived `profile_derived`（仅 `profile_backfill_v1=true` 的账号）> stored `realm`（`cn→cn-cli`、`global→intl-work`，保证 flag-off 等价）> domain > `cn-cli`。
- 因此 `www.codebuddy.ai` + `realm=global` 的旧账号仍走 `intl-work`；要使用 `intl-cli`/`cn-work` 必须显式设置 `profile`（或后续 backfill）。
- **Selector:** core `api_keys[].profiles`（数组，空 = 不限，`*` = 任意）与 `X-WB2A-Profile` 头；manager `api_keys.profile`（单值，空 = 不限），且必须 ⊆ 该上游 key 的 `profiles`。头值必须在 key 的 `profiles` 允许集内，否则 400 `profile_not_allowed`（校验点在 Todo 20/22 的选号入口）。
- **Realm header:** `X-WB2A-Realm` **仅用于 search/MCP**（无 model 的请求）；chat/Responses/Anthropic 继续用现有 `key.realm` vs 请求模型 realm 的校验，不读该头。manager 设置该头：有 key.realm 时用 key.realm；无 key.realm 时按 key.profile 推导（`intl-*→global`、`cn-*→cn`）；头缺失/空时 core 回落 key.realm 或 profile 推导，不强制要求头。**仅 search/MCP 入口**在 `key.Realm != ""` 且头值不等时返回 400 `key_realm_mismatch`；chat 路径忽略该头。
- **Header write site:** manager `gateway._upstream_headers(upstream, key=None)` 统一写入 `X-WB2A-Profile`/`X-WB2A-Realm`；值为空时不写该头；`key=None` 时（playground/admin 调试）不写。调用点必须全部更新：`playground.py:84`、`anthropic.py:1025,1061`、`responses.py:1640,1687`、`gateway.py:788,1006,1032`；`_map_model` 调用点：`gateway.py:531,976`、`responses.py:1596`、`anthropic.py:218,238,986,1224`；`_authorize` 返回签名变更的调用点：`gateway.py:775,977`、`responses.py:1597`、`anthropic.py:987,1225`。
- **Base override precedence（两个独立轴）**：① **profile 选择**：explicit > persisted derived（仅 `profile_backfill_v1=true`）> stored realm > domain > cn-cli。② **host 选择**：在 ① 选定 profile 后，若该 profile 的 realm 配置了 `upstream.cn_chat_base`（cn-*）/`global.chat_base`（intl-* chat）/`global.billing_base`（intl-* billing），**该配置 override 替换 profile 表默认 host（对 explicit profile 同样生效）**；否则用 profile 表默认。`webBase`（`client.go:999-1005`）同 ②；cn-cli 的 web base 固定 `www.workbuddy.cn`。
- **Auxiliary bases:** refresh/auth、`/v3/config`、tasks、travel、desktop 使用 **chat base**（cn-work→`www.workbuddy.cn`，intl-cli→`www.codebuddy.ai`）；`webBase` 使用 web base 列。
- `profiles.enabled=false` 时完全走旧 `chatBase(a)`/`globalOn(a)`；`detect_only=true` 只写字段，**URL 路由与选号过滤都不使用 profile**。

**2. Model route schema 与解析**

```sql
CREATE TABLE IF NOT EXISTS model_routes (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id         TEXT    NOT NULL,
  target_model      TEXT    NOT NULL,
  realm             TEXT    NOT NULL DEFAULT '',   -- '' | 'cn' | 'global'
  upstream_group_id INTEGER,                       -- NULL = 任意分组
  credits_override  REAL,                          -- NULL = 不覆盖；仅展示
  keep_original     INTEGER NOT NULL DEFAULT 1,
  enabled           INTEGER NOT NULL DEFAULT 1,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_model_routes
  ON model_routes(public_id, realm, COALESCE(upstream_group_id, 0));
```

解析顺序（`modelroutes.resolve`；每步至多一行，**无 priority**）：

1. `public_id + realm + group` 精确。
2. `public_id + realm + group IS NULL`。
3. `public_id + realm='' + group`。
4. `public_id + realm='' + group IS NULL`。
5. 兜底 `public_id='*'`：依次 `realm+group`、`realm+NULL`、`''+group`、`''+NULL`。
6. 表为空 → 旧 `model_map.target_for`；否则原样透传。

- `keep_original=1`：`/v1/models` 同时保留上游原 ID 与别名；`0`：只保留 `public_id`。
- `credits_override`：只影响 `/v1/models.credits` 展示与模型中心排序，**绝不影响** `quota_credit` 计费。
- **Tenant isolation:** 路由只按 `(public_id, realm, key_group)` 匹配，`key_group = key.upstream_id`，未绑定时为 `NULL`。绑定到其他分组的行对该 key **不可见**（不参与解析），因此不存在"路由指向组 2 却打到组 1"的二义性；不再记 `route_group_ignored`。
- **Backfill:** 只在 `settings['model_routes_backfill_v1']` 缺失且表为空时执行；快照 `model_map` 到 `settings['model_map_pre_routes']`；非 `*` 条目写 `realm=''`、`keep_original=1`、`credits_override=NULL`、`enabled=1`；`*` 写为兜底行。行数 = 非 `*` 条目数 +（有 `*` ? 1 : 0）；空 map = 0 行 + marker。
- **Rollback:** `DELETE FROM model_routes; DELETE FROM settings WHERE key IN ('model_routes_backfill_v1','model_map_pre_routes')`；旧 `model_map` 从未被修改。

**3. Billing JSON**

`GET /v1/dashboard/billing/subscription`（Bearer api_key；配额耗尽仍可读）：

```json
{"object":"billing_subscription","has_payment_method":false,"canceled":false,
 "canceled_at":null,"access_until":null,"soft_limit_usd":0,
 "hard_limit_usd":30.0,"system_hard_limit_usd":30.0,"codebuddy_balance_usd":12.34}
```

- `credit→USD` 只用 `billing_credit_price_usd`。
- `quota_credit=0`（不限）时：`hard_limit_usd=null`、`system_hard_limit_usd=null`、`codebuddy_balance_usd=null`；客户端应把 null 当"不限"。
- `quota_credit>0` 时：`hard_limit_usd=round2(quota_credit * price)`；`codebuddy_balance_usd=round2(max(0, quota_credit - used_credit) * price)`。
- `access_until = key.expires_at or null`（示例 null 是占位）。
- 仅当 `billing_currency=cny` 时额外输出 `codebuddy_balance_cny = round2(codebuddy_balance_usd * billing_usd_rate)`；`billing_usd_rate` 不参与 USD 字段。

`GET /v1/dashboard/billing/usage?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`：

```json
{"object":"list","total_usage":123,
 "daily_costs":[{"timestamp":1790000000,
   "line_items":[{"name":"deepseek-v4-flash","cost":123}]}]}
```

- `total_usage` 单位**美分**：**每个 `daily_costs[].line_items[].cost` 先 `round_half_up(line_credit * price * 100)`，`total_usage` = 所有 line_item cost 之和**（不再用全局 sum 再 round，避免分项与总计对不上）。
- 默认最近 30 天；`end_date >= start_date`；最大 365 天；非法 400。
- `/v1/usage` 保持不变，文档说明两者关系。

**4. Search / MCP**

`POST /v1/search`：`{"query":"...","max_results":5}` → `{"object":"search_result","results":[{"title","url","snippet"}]}`；`max_results` 1-10，默认 5。
`POST /v1/mcp`：JSON-RPC 2.0，无状态；方法 `initialize` / `tools/list` / `tools/call`；工具 `web_search`；协议版本 `2026-07-28`、`2025-11-25`、`2025-06-18`；带 `Origin` 头 403；无 `Mcp-Session-Id`。
- 鉴权与顺序：Bearer api_key；**manager tools 入口先执行全局 IP 规则（`security.get_security_config()` + `iputil.evaluate`，与 `_authorize` 同源）再解析 key；黑名单 IP + 缺失/无效/禁用 key → 403 `ip_blocked`，不写 `request_logs`、不打上游**；manager 设置 `X-WB2A-Realm`/`X-WB2A-Profile`；core 校验 realm 与 key.realm（头缺失或空时回落 key.realm）、profile 与 key.profiles；选号用 realm + key `groups` + profile。**realm 推导（唯一链）**：header profile > key.realm > 单一 realm 的 key.profiles > `cn`；**key.profiles 跨 realm 且无 header/realm → 400 `realm_required`**。
- 模型白名单不适用；限流见下。
- **Rate limit:** **manager 每下游 key 60 次/分钟**（键 = SHA-256(manager key id)）；**core 每上游 key 600 次/分钟**（键 = SHA-256(Authorization token)，不信任客户端头）；进程内固定窗口（60s）；超限 429 `rate_limit_exceeded` + `Retry-After: 60`；写入站日志但不写上游请求日志；多实例各自计数（明确接受近似）。
- **Core logging:** 写 `reqlog.Event{Model:"web_search"/"mcp", HasCredit:false}` 并 `usage.AddKeyed`；core **没有** `request_logs` 表。manager 代理侧才写 `request_logs`（tokens=0，credit NULL）。
- **MCP `tools/call`** 必须复用 Todo 26 提取的 `pickForSearch(r, key)` helper（realm/groups/profile），并测 two-profile/two-group。

**5. Feature flag inventory**

| flag | repo | storage | default | off behavior |
|---|---|---|---|---|
| `model_routes_enabled` | manager | `settings` | `false` | 走旧 `model_map` |
| `log_retention_overrides` | manager | `settings` | `{}` | 用现有常量 |
| `responses_projection_mode` | manager | `settings` | `passthrough` | 不投影 |
| `responses_projection_max_item_bytes` | manager | `settings` | `40000` | 仅 balanced 生效 |
| `tool_repair` | manager | `settings` | `false` | 保留非法 JSON |
| `tool_repair_recall` | manager | `settings` | `false` | 不重调模型 |
| `billing_credit_price_usd` | manager | `settings` | `0.03` | 仅影响 billing 端点 |
| `billing_usd_rate` | manager | `settings` | `7.15` | 仅 cny 展示 |
| `billing_currency` | manager | `settings` | `usd` | 不输出 cny 字段 |
| `catalog_group_merge` | manager | `settings` | `false` | 不输出 `available_in_groups` |
| `tools_search_enabled` | manager | `settings` | `false` | `/v1/search` 404 |
| `tools_mcp_enabled` | manager | `settings` | `false` | `/v1/mcp` 404 |
| `upstream.max_images` | core | `config.json` + livecfg | `0` | 不限 |
| `upstream.image_policy` | core | `config.json` + livecfg | `passthrough` | 不改写 |
| `upstream.capability_guard` | core | `config.json` + livecfg | `false` | 不预检 |
| `profiles.enabled` | core | `config.json`（restart） | `false` | 旧 realm 路由 |
| `profiles.detect_only` | core | `config.json`（restart） | `true` | 只展示 |
| `search.enabled` | core | `config.json` + livecfg | `false` | 404 |
| `mcp.enabled` | core | `config.json` + livecfg | `false` | 404 |
