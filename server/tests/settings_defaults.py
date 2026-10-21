"""19 个功能开关的**单一事实来源**（测试辅助模块，不改变运行时行为）。

为什么需要这个文件
------------------
契约 §5（`docs/migration/codebuddy2api-contracts.md`「5. Feature flag inventory」）
把 19 个开关分散在**两个仓库**里：manager 的 `settings` 表与 core 的
`config.json` + livecfg。F4（scope fidelity 门禁）要逐条比对「实现里的默认值」与
契约，如果每个 todo 各自在自己文件里写一份默认值，比对就成了「猜哪一份是真的」——
漂移出来的表现正是这次迁移最怕的东西：flag 默认开着、行为却和 golden 不一致。

所以这里集中定义：名称、所属仓库、存储位置、默认值。**只被测试与门禁脚本导入**，
产品代码不 import 它（避免测试模块进入运行时依赖链）。

解析约定（供 F4 使用）
----------------------
`parse_defaults(text)` 接受两种输入，都是机器可读的：

  · 本模块导出的文本：`flag_defaults_text()` 的输出（`name=json 字面量` 每行一条）；
  · 契约文档里的 markdown 表格行（`| \\`flag\\` | repo | storage | default | ... |`）。

两种都归一到同一个 `{flag: value}` 字典，比较时用 `==`（值已按 JSON 解析，
`false` 是 bool、`40000` 是 int、`{}` 是 dict、`"passthrough"` 是 str）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

# ── 19 个开关：名称 → (所属仓库, 存储位置, 默认值) ─────────────────────
# 顺序与契约 §5 表格一致；默认值逐字取自该表。
FLAG_SPECS: dict[str, tuple[str, str, Any]] = {
    'model_routes_enabled': ('manager', 'settings', False),
    'log_retention_overrides': ('manager', 'settings', {}),
    'responses_projection_mode': ('manager', 'settings', 'passthrough'),
    'responses_projection_max_item_bytes': ('manager', 'settings', 40000),
    'tool_repair': ('manager', 'settings', False),
    'tool_repair_recall': ('manager', 'settings', False),
    'billing_credit_price_usd': ('manager', 'settings', 0.03),
    'billing_usd_rate': ('manager', 'settings', 7.15),
    'billing_currency': ('manager', 'settings', 'usd'),
    'catalog_group_merge': ('manager', 'settings', False),
    'tools_search_enabled': ('manager', 'settings', False),
    'tools_mcp_enabled': ('manager', 'settings', False),
    'upstream.max_images': ('core', 'config.json + livecfg', 0),
    'upstream.image_policy': ('core', 'config.json + livecfg', 'passthrough'),
    'upstream.capability_guard': ('core', 'config.json + livecfg', False),
    'profiles.enabled': ('core', 'config.json (restart)', False),
    'profiles.detect_only': ('core', 'config.json (restart)', True),
    'search.enabled': ('core', 'config.json + livecfg', False),
    'mcp.enabled': ('core', 'config.json + livecfg', False),
}

# 扁平视图：门禁与测试最常需要的两个映射。
FLAG_DEFAULTS: dict[str, Any] = {name: spec[2] for name, spec in FLAG_SPECS.items()}
FLAG_OWNERS: dict[str, str] = {name: spec[0] for name, spec in FLAG_SPECS.items()}
FLAG_STORAGE: dict[str, str] = {name: spec[1] for name, spec in FLAG_SPECS.items()}

# 契约 §5 的 flag 总数（写死成常量：少一条/多一条都该让门禁立刻失败）。
FLAG_COUNT = 19


def owner_of(flag: str) -> str:
    """该 flag 由哪个仓库拥有（'manager' | 'core'）。未知 flag 抛 KeyError。"""
    return FLAG_SPECS[flag][0]


def default_of(flag: str) -> Any:
    """该 flag 的契约默认值。未知 flag 抛 KeyError（不静默返回 None）。"""
    return FLAG_SPECS[flag][2]


def flags_owned_by(repo: str) -> list[str]:
    """按仓库取 flag 名单（保持契约表格顺序）。"""
    return [name for name, spec in FLAG_SPECS.items() if spec[0] == repo]


def _json_scalar(raw: str) -> Any:
    """把契约表格里的字面量转成 Python 值。

    契约表里 `false` / `40000` / `{}` / `"passthrough"` / `usd` 混排，且默认值
    那一列带反引号（markdown 代码样式）。先去反引号，再试 JSON —— 前三类是
    JSON 字面量，`usd` 这类裸词是字符串。
    """
    text = raw.strip().strip('`').strip()
    try:
        return json.loads(text)
    except ValueError:
        return text


def flag_defaults_text() -> str:
    """机器可读的默认值清单（F4 可直接 `parse_defaults()` 回读）。"""
    lines = ['# codebuddy2api contract §5 — 19 feature flags (name=default)']
    for name, value in FLAG_DEFAULTS.items():
        lines.append(f'{name}={json.dumps(value, ensure_ascii=False)}')
    return '\n'.join(lines) + '\n'


_TABLE_ROW = re.compile(r'^\|\s*`(?P<flag>[^`]+)`\s*\|(?P<rest>.*)\|\s*$')


def parse_table(text: str) -> dict[str, Any]:
    """从契约文档的 markdown 表格里解析 `{flag: default}`。

    只认 5 列的 flag 表格行：第 1 列是反引号包裹的名字、第 2 列是仓库名
    （`manager` / `core`）、第 4 列是 default。

    为什么必须按「第 2 列是仓库名」过滤：契约文档里还有别的 5 列表格（profile
    的 base URL 表，第 1 列同样带反引号），只按列形状匹配会把 `cn-work` /
    `intl-cli` 之类当成 flag —— 门禁就会报「契约之外的 flag」，而那是文档里的
    另一张表。判据是**列语义**，不是列数。
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        m = _TABLE_ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group('rest').split('|')]
        # cells = [repo, storage, default, off-behavior]
        if len(cells) < 3 or cells[0].strip('`') not in ('manager', 'core'):
            continue
        out[m.group('flag')] = _json_scalar(cells[2])
    return out


def parse_defaults(text: str) -> dict[str, Any]:
    """解析默认值文本：先按 `name=value` 逐行，再回落到 markdown 表格。

    两条路径都不产生空字典之外的副作用；返回值只含 19 个 flag 里出现过的键，
    调用方据此判断「契约里有、实现里没有」或反之。
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if '=' in stripped and '|' not in stripped:
            name, _, raw = stripped.partition('=')
            name = name.strip()
            if name:
                out[name] = _json_scalar(raw)
    if out:
        return out
    return parse_table(text)


def assert_contract(parsed: dict[str, Any]) -> list[str]:
    """比对一份解析结果与契约默认值，返回人类可读的差异列表（空 = 一致）。

    只做「集合与取值」比对，不改任何东西——F4 用它输出失败原因。
    """
    problems: list[str] = []
    missing = sorted(set(FLAG_DEFAULTS) - set(parsed))
    extra = sorted(set(parsed) - set(FLAG_DEFAULTS))
    if missing:
        problems.append(f'缺少 flag 默认值：{missing}')
    if extra:
        problems.append(f'契约之外的 flag：{extra}')
    for name in sorted(set(FLAG_DEFAULTS) & set(parsed)):
        want = FLAG_DEFAULTS[name]
        got = parsed[name]
        if got != want:
            problems.append(f'{name} 默认值 {got!r} != 契约 {want!r}')
    return problems


def iter_flags(repo: str | None = None) -> Iterable[tuple[str, str, str, Any]]:
    """按 (flag, repo, storage, default) 迭代，可按仓库过滤。"""
    for name, (owner, storage, default) in FLAG_SPECS.items():
        if repo is None or owner == repo:
            yield name, owner, storage, default
