# 迁移回滚 Runbook（codebuddy2api 迁移）

本文件覆盖 2026-10 迁移引入的**全部可回滚面**：模型路由、产品 profile、
search/MCP、保留策略、计费、Responses 投影/工具修复、账号文件权限与
auth base 回落。原则：**先关开关，再动数据；先备份，再恢复**。

> 不要直接 `rm data/manager.db`。迁移只做加法（新表/新列/新 settings 键），
> 旧代码可以继续读旧字段；删库会同时丢掉账号备注、密钥、审计与日志。

## 0. 先备份（任何回滚的第一步）

```bash
cd /opt/workbuddy-manager            # 或你的部署目录
mkdir -p data/backups
STAMP=$(date +%F-%H%M%S)
sqlite3 data/manager.db ".backup 'data/backups/manager-$STAMP.db'"
cp data/users.json "data/backups/users-$STAMP.json"
ls -l "data/backups/manager-$STAMP.db"
```

预期：`data/backups/manager-<时间>.db` 存在且大小与 `data/manager.db` 接近。
没有 `sqlite3` 时用 Python：

```bash
python3 - <<'PY'
import sqlite3
src = sqlite3.connect('data/manager.db')
dst = sqlite3.connect('data/backups/manager-manual.db')
src.backup(dst)
dst.close(); src.close()
PY
```

## 1. 模型路由（`model_routes` / `model_routes_enabled`）

**只关功能（推荐，可热生效）：**

```bash
curl -s -X POST http://127.0.0.1:7864/api/settings/model-routes/config \
  -H "Authorization: Bearer <admin-session-or-token>" \
  -H 'Content-Type: application/json' -d '{"enabled": false}'
```

预期：`{"enabled": false}`；之后 `GET /api/settings/model-routes` 仍能看到
旧行（只是不生效），`/v1/models` 与 chat 回到 `model_map` / 原始模型名行为。

**彻底移除路由表（不可逆，先备份）：**

```bash
sqlite3 data/manager.db "DROP TABLE IF EXISTS model_routes;"
```

预期：`sqlite3 data/manager.db ".tables"` 不再列出 `model_routes`；
旧代码不依赖该表，服务可正常启动。

## 2. 产品 profile（core `profiles.*`）

core 的 `profiles.enabled` / `profiles.detect_only` 只在**启动时**读取，
改完必须重启 core 容器：

```bash
# core config.json
python3 - <<'PY'
import json
p = '/opt/workbuddy2api/config.json'
d = json.load(open(p))
d.setdefault('profiles', {})['enabled'] = False
d['profiles']['detect_only'] = True
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
PY
docker restart workbuddy2api
```

预期：`docker logs --tail 20 workbuddy2api` 出现配置加载日志；chat 的
URL/头集合回到迁移前 golden（cn → copilot.tencent.com，global →
www.workbuddy.ai），选号忽略 `key.profiles` / `X-WB2A-Profile`。

## 3. search / MCP（manager + core）

manager 侧两个开关（`tools_search_enabled` / `tools_mcp_enabled`）与 core
`search.enabled` / `mcp.enabled` 一起关：

```bash
# manager 设置页：设置 → 上游 → 搜索 / MCP 开关关闭；
# 或直接调设置接口（仅默认上游）
curl -s -X POST http://127.0.0.1:7864/api/settings/upstream \
  -H "Authorization: Bearer <admin-session>" -H 'Content-Type: application/json' \
  -d '{"search":{"enabled":false},"mcp":{"enabled":false}}'
```

预期：`/v1/search` 与 `/v1/mcp` 从 200/400 变回 **404**（路由不存在）；
manager 的 `/v1/search`、`/v1/mcp` 代理同样 404；不影响 chat。

## 4. 图片策略 / 能力预检（core `upstream.*`）

```bash
# core config.json
python3 - <<'PY'
import json
p = '/opt/workbuddy2api/config.json'
d = json.load(open(p))
u = d.setdefault('upstream', {})
u['max_images'] = 0
u['image_policy'] = 'passthrough'
u['capability_guard'] = False
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
PY
docker restart workbuddy2api
```

预期：带图请求不再被 413/400 拦截；payload 与迁移前 golden 逐字一致。

## 5. 保留策略 / 计费 / Responses（manager settings）

这些是 manager `settings` 表里的键，关掉即回到旧行为：

```bash
# 保留策略恢复默认（删除 override）
curl -s -X POST http://127.0.0.1:7864/api/settings/log-retention \
  -H "Authorization: Bearer <admin-session>" -H 'Content-Type: application/json' -d '{}'

# 计费恢复默认
curl -s -X POST http://127.0.0.1:7864/api/settings/billing \
  -H "Authorization: Bearer <admin-session>" -H 'Content-Type: application/json' \
  -d '{"billing_credit_price_usd":0.03,"billing_usd_rate":7.15,"billing_currency":"usd"}'

# Responses 投影/修复恢复默认
curl -s -X POST http://127.0.0.1:7864/api/settings/responses \
  -H "Authorization: Bearer <admin-session>" -H 'Content-Type: application/json' \
  -d '{"projection_mode":"passthrough","max_item_bytes":40000,"tool_repair":false,"tool_repair_recall":false}'
```

预期：`/v1/responses` 回到 passthrough；工具参数不再改写；日志
`tool_repair` 恒为 `none`。

## 6. 数据库恢复（快照损坏 / 迁移失败）

```bash
docker stop workbuddy-manager
cp data/manager.db "data/manager.db.broken-$(date +%s)"
cp "data/backups/manager-<时间>.db" data/manager.db
docker start workbuddy-manager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7864/healthz
```

预期：`200`；账号、密钥、日志与备份时刻一致。快照缺失时**明确报错**并
保留 `data/manager.db.broken-*` 供人工恢复，不要清空目录。

## 7. 账号文件权限（跨 uid 折衷）

| 写入方 | 新文件默认 | 已有文件 |
|---|---|---|
| manager（`tencent.write_auth_file`） | `0644` | 保留原 mode/uid/gid |
| core（`auth.SaveAtomic`） | `0600` | 保留原 mode/uid/gid |
| `auth.file_mode=0640` | `0640` | 不放宽已有文件 |

跨 uid（manager 以 root 写、core 以 uid 10001 读）时推荐：

```bash
# core config.json
{"auth": {"file_mode": "0640"}}
# 宿主机：让两个进程同组
chgrp -R workbuddy /opt/workbuddy2api/auths
chmod 2750 /opt/workbuddy2api/auths
```

预期：manager 新建文件 0640，core refresh 后仍 0640；不要用 `chmod 0666`
绕过 EPERM（那会把凭据暴露给同机所有用户）。

## 8. auth base 回落（refresh）

refresh 的候选序列固定为 `[AuthBase, LegacyChatBase]`，**没有 override 键**：

- 先打 profile 表的 `AuthBase`；
- 4xx / 网络错误时回落 `LegacyChatBase`（cn → copilot.tencent.com，
  global → www.workbuddy.ai）；
- 两个都失败才报 refresh 失败。

排障：`docker logs workbuddy2api | grep -i "refresh.*fallback"`。若 AuthBase
不可达但 Legacy 可用，日志会出现 fallback；这是预期行为，不是错误。

## 9. 19 个开关的关闭清单

| # | flag | 仓库 | 关闭值 |
|---|---|---|---|
| 1 | `model_routes_enabled` | manager | `false` |
| 2 | `log_retention_overrides` | manager | `{}` |
| 3 | `responses_projection_mode` | manager | `passthrough` |
| 4 | `responses_projection_max_item_bytes` | manager | `40000` |
| 5 | `tool_repair` | manager | `false` |
| 6 | `tool_repair_recall` | manager | `false` |
| 7 | `billing_credit_price_usd` | manager | `0.03` |
| 8 | `billing_usd_rate` | manager | `7.15` |
| 9 | `billing_currency` | manager | `usd` |
| 10 | `catalog_group_merge` | manager | `false` |
| 11 | `tools_search_enabled` | manager | `false` |
| 12 | `tools_mcp_enabled` | manager | `false` |
| 13 | `upstream.max_images` | core | `0` |
| 14 | `upstream.image_policy` | core | `passthrough` |
| 15 | `upstream.capability_guard` | core | `false` |
| 16 | `profiles.enabled` | core | `false` |
| 17 | `profiles.detect_only` | core | `true` |
| 18 | `search.enabled` | core | `false` |
| 19 | `mcp.enabled` | core | `false` |

manager 的键可用设置页/接口改；core 的键改 `config.json` 后重启
`workbuddy2api`（`profiles.*` 只在启动时读，其余 livecfg 可热生效）。

## 10. 演练记录

见 `.omo/evidence/codebuddy2api-migration/task-31-rollback.log`：临时库中
插入一条 `model_routes` → 关闭开关 → 删除行 → 从备份恢复，四步均成功。
