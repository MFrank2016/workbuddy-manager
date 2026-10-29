# 上游补丁目录（已退役）

**不要在这里新增补丁。** 上游 `workbuddy2api` 的改动现在直接进入 core 仓库
[`MFrank2016/workbuddy2api`](https://github.com/MFrank2016/workbuddy2api)，由 core tag
（当前为 `manager-core-2026.10.27`）统一发布。

## 发生了什么

- 本目录里编号 `0001`–`0030` 的补丁（本分支现存 `0001`–`0019`，`0020`–`0030`
  来自后续发布线）已经全部迁入 core tag `manager-core-2026.10.27` 的源码。
- 管理端发布流程不再读取本目录：`.github/workflows/release.yml` 直接取 core tag
  的源码资产，校验 `upstream/.core-tag` 后打进发布包。
- 这些文件**保留不动**，用于复现旧 tag 的历史构建；不要删除、改写或重新编号。

## 新改动怎么做

1. 在 core 仓库改代码、补 Go 测试，提 PR；
2. 合并后打新的 core tag，并按 `docs/release-process.md` 发布源码资产
   （资产必须带 `.core-tag` 标记）；
3. 在管理端把 `.github/workflows/release.yml` 的 `CORE_TAG` 指向新 tag。

不要再往管理端仓库提交 `*.patch`。面板侧若需要跟随上游字段变化，按正常的管理端
改动提 PR，并在 `docs/release-process.md` 的「仍然要守的老规矩」里核对两条耦合路径。

## 历史构建

`dev/apply_upstream_patches.py` 仍然保留，但启动时会向 stderr 打印退役提示。
它只用于按旧 `upstream-src` 资产 + 本目录补丁复现历史版本，不是当前发布路径。
