# 上游风控限制不再永久禁用 + 大上下文请求逐请求轮换

两个改动解决的是同一类现场问题：**账号池被上游风控逐个打掉，而网关自己的处理把
「临时限制」变成了「永久出池」。**

---

## 一、11140「账号异常行为」按软冷却处理（0043）

### 现场

CN 账号调用 `/v2/chat/completions` 返回：

```json
{"code":11140,"msg":"request illegal","requestId":"...",
 "displayMsg":{"zh":"账号存在异常行为，已被限制使用","en":"Your account has been restricted due to unusual activity"},
 "displayTips":{"zh":"系统检测到该账号存在异常使用行为，如需申诉请提交反馈"},
 "actions":["EDIT_INPUT","SUBMIT_FEEDBACK"]}
```

判定依据（同一现场实测）：

| 观察 | 结论 |
| --- | --- |
| 23 个账号全部同出口 IP，其中 17 个正常 200 | 不是 IP 被封 |
| 无 UA、无会话头、正文 `ping` 的极简请求也返回 11140 | 不是逐请求的 UA/指纹校验 |
| 非 chat 接口（模型目录）仍 200 | 凭证没坏，限制只作用在 chat |
| 同一账号数小时后恢复 200 | 限制会自行解除，可申诉 |

而 `applyErrorPolicy` 把 `request illegal` 一律 `Disable`，并写
`re-login required` —— 6 个还能恢复的号被永久踢出池子，面板显示「已封禁，需重新登录」。

### 改动

- 新增 `upstream.IsUpstreamRestriction(body)`：**只认带风控话术的 11140**
  （`unusual activity` / `异常行为` / `異常行為` / `abnormal`）。
- 命中时按账号级软冷却 45 分钟，理由 `upstream restriction (code 11140, http 403, appealable)`，
  **不 Disable、不清凭证**。
- 不带风控话术的 `request illegal`（历史 global `auth_forbidden` 语义）**行为不变**，仍硬禁用。

### 验证

| 项 | 结果 |
| --- | --- |
| 线上：12 条大请求（必然踩到 2 个受限号） | **12/12 200**；2 个号进入 **45 分钟软冷却**，`disabled: 0` |
| 单测 | `TestIsUpstreamRestriction`（5 例）、`TestApplyErrorPolicyUpstreamRestrictionCools`、`TestApplyErrorPolicyPlainRequestIllegalStillDisables` |

---

## 二、大上下文请求逐请求轮换（0044）

### 现场

被封的 6 个号，最后几条请求形态完全一致：

```
key=38 / UA=deepseek-harness / 客户端 IP 222.85.110.6
单请求 prompt = 610k–650k token（≈2.4–6 MB 报文，含 tool 截图）
间隔 7–14 秒连发，且在账号池里轮换
```

也就是说：**单个号在几十秒内被连续压了多个超大上下文请求**，上游把这种形态判为
「异常使用行为」。除了「别让一个号短时间吃这么多大请求」，没有更低风险的改法。

### 改动

`config.pool` 新增两个键：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `large_request_bytes` | `262144`（256 KiB） | 请求体 ≥ 该值视为大上下文请求；`<=0` 关闭 |
| `large_request_cooldown` | `"90s"` | 窗口内不再把大请求给同一个号 |

请求体超阈值时：

1. **放弃会话粘性**（不按会话绑号，成功后也不写绑定）；
2. 选号时排除窗口内已承接过大请求的号 → **逐请求轮换**；
3. 被选中即登记（无论成败），保证窗口内不复用；
4. 若所有号都在窗口内，退化为普通轮换，避免直接 503。

小请求（< 阈值）行为完全不变。

### 验证

| 项 | 结果 |
| --- | --- |
| 连发 10 条 300 KB 请求 | **10 个不同账号、各 1 次**，全部 200 |
| 连发 10 条小请求（同 UA） | 仍集中 1 个账号（粘性未变） |
| 单测 | `TestLargeExclusionsWindow` / `TestLargeExclusionsDisabled` |

---

## 应用方式

```bash
cd upstream   # v2.11.6 源码根
git apply /path/to/0043-upstream-restriction-soft-cooldown.patch
git apply /path/to/0044-large-request-rotation.patch
go build ./... && go test ./internal/server/ ./internal/upstream/
```

两个补丁互相独立（0044 不依赖 0043），都基于 v2.11.6 上游源码。

## 未纳入本 PR 的本地改动

本部署另有 `MaxRotate 3→6` 与「401 先刷新再计数」两项本地增强，与本次两个缺陷无关，
未包含在本 PR 中。