"""工具调用参数的确定性修复（`tool_repair`，契约 §5 / Todo 15）。

为什么是**确定性**修复而不是「再问一次模型」
--------------------------------------------
上游偶尔把工具参数吐成非法 JSON：带 markdown fence、多一个尾逗号、少了右括号。
这些形态有唯一的最小修复方式，不需要（也不应该）让模型重写一遍——重写会
**发明参数值**，而工具调用最怕的就是「看起来能跑，其实参数是编的」。

参考实现（codebuddy2api）走的是整请求重试，不是 JSON 修复。本模块只做它之外
的确定性部分；`recall`（最多一次额外上游调用）是它之上的可选补偿，见
`recall_arguments`。两条协议（Responses / Anthropic）共用这里，避免各写一份
判据而出现「同一个坏参数在一个协议修好、在另一个协议没修」。

铁律：**绝不发明值**。修不好就把原字符串原样返回（状态 `failed`），由调用方
决定是否 recall / 是否记日志——不塞 `{}`、不补默认字段、不猜截断处的内容。
"""
from __future__ import annotations

import copy
import json
import logging
import re

from . import db

logger = logging.getLogger('workbuddy.toolrepair')

# 契约 §5 的枚举值：`none|fixed|failed|recalled`。
STATUS_NONE = 'none'
STATUS_FIXED = 'fixed'
STATUS_FAILED = 'failed'
STATUS_RECALLED = 'recalled'

# recall 请求附带的 system 指令（契约 §5 原文）。
TOOL_REPAIR_RECALL_INSTRUCTION = 'Return valid JSON arguments only for tool calls'

# markdown fence：可选的语言标记（```json / ```JSON），内容到收尾 fence 或结尾。
_FENCE = re.compile(r'```[A-Za-z0-9_.+-]*[ \t]*\r?\n?(.*?)(?:```|\Z)', re.S)


def _is_json_object(text: object) -> bool:
    if not isinstance(text, str):
        return False
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        return False


def _remove_trailing_commas(text: str) -> str:
    """去掉 `,}` / `,]` 前的逗号。字符串里的逗号不动（按引号/转义扫描）。"""
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ',':
            look = index + 1
            while look < len(text) and text[look] in ' \t\r\n':
                look += 1
            if look < len(text) and text[look] in '}]':
                index += 1  # 丢掉这个逗号
                continue
        out.append(char)
        index += 1
    return ''.join(out)


def _balance(text: str) -> str | None:
    """补齐缺失的 `}` / `]`；结构错乱（错配、字符串未收尾）返回 None。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {'{': '}', '[': ']'}
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in '}]':
            if not stack or stack.pop() != char:
                return None
    # 字符串没闭合 = 内容可能被截断，补引号等于替模型决定「值到哪儿为止」。
    if in_string:
        return None
    if not stack:
        return text
    return text + ''.join(reversed(stack))


def _first_object(text: str) -> str | None:
    """提取第一个**完整且可解析**的 JSON 对象（跳过前后的散文）。"""
    for start in range(len(text)):
        if text[start] != '{':
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    if _is_json_object(candidate):
                        return candidate
                    break
    return None


def _strip_fence(text: str) -> str:
    match = _FENCE.search(text)
    return match.group(1).strip() if match else text


def repair_tool_arguments(raw: object) -> tuple[object, str]:
    """尝试修复一段工具参数字符串。

    返回 `(值, 状态)`：

      · 已是合法 JSON 对象 / 不是字符串 → `(原值, 'none')`（没动过）；
      · 修好了 → `(修复后的字符串, 'fixed')`；
      · 修不好 → `(原字符串, 'failed')`，**逐字未变**。
    """
    if not isinstance(raw, str):
        return raw, STATUS_NONE
    if _is_json_object(raw):
        return raw, STATUS_NONE
    text = raw.strip()
    if '```' in text:
        text = _strip_fence(text)
    no_commas = _remove_trailing_commas(text)
    candidates = [text, no_commas]
    for base in (text, no_commas):
        first = _first_object(base)
        if first is not None:
            candidates.extend([first, _remove_trailing_commas(first)])
        balanced = _balance(base)
        if balanced is not None:
            candidates.extend([balanced, _remove_trailing_commas(balanced)])
    for candidate in candidates:
        if _is_json_object(candidate):
            return candidate, STATUS_FIXED
    return raw, STATUS_FAILED


# ── 开关（契约 §5）────────────────────────────────────────────
TOOL_REPAIR_KEY = 'tool_repair'
TOOL_REPAIR_RECALL_KEY = 'tool_repair_recall'


def flags() -> tuple[bool, bool]:
    """读两个开关。非 True 一律当关闭（字符串 "true" 不算开启）。"""
    return (db.get_setting(TOOL_REPAIR_KEY, False) is True,
            db.get_setting(TOOL_REPAIR_RECALL_KEY, False) is True)


def repair_calls(data: dict, custom_tool_names: set[str]) -> tuple[dict, str]:
    """修复 `data` 里所有工具调用的参数，返回 `(data, 总体状态)`。

    总体状态取**最坏**的那个：只要有一个 `failed` 就整体放弃并返回**原始**
    data（不返回「一半改写、一半原样」的混合体——调用方拿到的既不是原响应也
    不是可用响应，而日志只能记一个状态）。custom tool 的入参是自由文本而非
    JSON，不参与修复。
    """
    enabled, _recall = flags()
    if not enabled or not isinstance(data.get('choices'), list):
        return data, STATUS_NONE
    repaired = copy.deepcopy(data)
    statuses: list[str] = []
    for choice in repaired['choices']:
        message = choice.get('message') if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        for call in message.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            fn = call.get('function')
            if not isinstance(fn, dict):
                continue
            if str(fn.get('name') or '') in custom_tool_names:
                continue
            value, status = repair_tool_arguments(fn.get('arguments'))
            statuses.append(status)
            if status == STATUS_FIXED:
                fn['arguments'] = value
    if STATUS_FAILED in statuses:
        return data, STATUS_FAILED
    if STATUS_FIXED in statuses:
        return repaired, STATUS_FIXED
    return data, STATUS_NONE


def recall_payload(payload: dict) -> dict:
    """recall 请求体：同一 body + system 指令（契约 §5 原文）。

    插在 messages 最前面而不是替换原 system：原 system 可能带着客户端的行为
    约束（缓存前缀也是它的一部分），丢掉等于换了个助手。
    """
    out = copy.deepcopy(payload)
    messages = out.get('messages')
    if not isinstance(messages, list):
        messages = []
    out['messages'] = [{'role': 'system', 'content': TOOL_REPAIR_RECALL_INSTRUCTION},
                       *messages]
    return out


async def recall_arguments(payload: dict, data: dict, custom_tool_names: set[str],
                           fetch) -> tuple[dict, str, object | None]:
    """参数修不好时最多**一次**额外上游调用。

    返回 `(最终 data, 状态, recall 的取数结果)`。`fetch` 是
    `async (payload) -> (resp, data, usage, latency)`：只负责打上游并解析，
    记日志交给调用方——那一行要等 recall 的结果出来才知道该写什么状态。
    修好了返回 `recalled`；仍修不好返回原 `data` 与 `failed`。
    """
    enabled, recall = flags()
    if not enabled or not recall:
        return data, STATUS_FAILED, None
    try:
        observed = await fetch(recall_payload(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning('工具参数 recall 失败（返回原参数）: %s', exc)
        return data, STATUS_FAILED, None
    retried = observed[1] if isinstance(observed, tuple) and len(observed) > 1 else None
    if not isinstance(retried, dict):
        return data, STATUS_FAILED, observed
    repaired, status = repair_calls(retried, custom_tool_names)
    # recall 回来的参数本来就合法时 status=none —— 那也是 recall 成功了。
    if status in (STATUS_FIXED, STATUS_NONE):
        return repaired, STATUS_RECALLED, observed
    return data, STATUS_FAILED, observed
