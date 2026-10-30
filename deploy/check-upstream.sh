#!/usr/bin/env bash
# ============================================================
# 上游检查（保留命令，但不再访问外部仓库）
#
# 上游 workbuddy2api 由 core 仓库（MFrank2016/workbuddy2api）维护，源码以 core tag
# 资产随管理端 Release 分发；没有可比较的第三方上游提交。发版前应验证的是
# core tag 与 Go 测试，而不是 git 提交：
#
#   git -C ../workbuddy2api show manager-core-2026.10.29 --stat
#   (cd <解包后的 upstream> && go build ./... && go test ./...)
#
# 退出码：0 = 无需外部检查（可以继续发版流程）
# ============================================================
set -euo pipefail

echo "[—] 上游已由 core tag 维护，随管理端 Release 分发；不再检查外部仓库提交。"
echo "    发版前请确认 core tag 已发布，并跑 Go 测试（见 docs/release-process.md）。"
exit 0
