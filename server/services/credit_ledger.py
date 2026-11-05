"""上游逐笔积分账本（core `/panel/api/credit_history`）的拉取与规范化。

为什么单独一层：`/api/stats/credits` 的收入口径从「余额快照差分」改为
「上游逐笔正 delta」，但 stats 层不该知道 HTTP / 鉴权 / 上游响应形状。这里只做：

  · 取满上游 limit 上限（1000 条，账本本身保留 2000 条）；
  · 把每条带时区的 ISO 时间换算成本地 day / hour，保留 uid 与 delta（含负值，
    负值是消耗，覆盖期判定要用全部条目）；
  · 上游不可达 / 响应异常 → 返回 (False, [])，由调用方降级，绝不抛到路由层。

上游账本只保留最近若干天（实测 6 天左右、且被 1000 条上限截断），所以调用方
必须把覆盖期如实标出来，不能假装它是完整历史。
"""

from __future__ import annotations

import datetime
import logging

from . import panelapi

logger = logging.getLogger(__name__)

# 上游 panel.go creditHistory 把 limit 钳到 1000；取满才能拿到最长覆盖期。
_LEDGER_LIMIT = 1000
# 上游账本本身的上限（credithist.defaultMaxEntries），仅用于注释/日志语义。
_PATH = "/panel/api/credit_history"


async def fetch_entries(upstream_id: int | None = None):
    """返回 (是否可用, 规范化后的条目列表)。

    条目形如 `{'day': 'YYYY-MM-DD', 'hour': 0-23, 'uid': str, 'delta': int|float}`。
    单条解析失败（缺 time / 时间非法 / delta 非数值）静默跳过，不影响其它行。
    """
    try:
        up = panelapi.resolve_upstream(upstream_id)
        data = await panelapi.call(up, "GET", f"{_PATH}?limit={_LEDGER_LIMIT}")
    except Exception as exc:  # noqa: BLE001 - 上游不可达时降级，不打断统计页
        logger.warning("积分账本不可用（收入将显示为空）: %s", exc)
        return False, []
    if not isinstance(data, dict):
        logger.warning("积分账本返回非对象响应，按不可用处理")
        return False, []

    raw = data.get("entries")
    if not isinstance(raw, list):
        return True, []

    out = []
    for item in raw:
        parsed = _parse_entry(item)
        if parsed is not None:
            out.append(parsed)
    return True, out


def _parse_entry(item: object) -> dict | None:
    """把一条上游账本记录解析成本地 day/hour + uid + delta。"""
    if not isinstance(item, dict):
        return None
    raw_time = item.get("time")
    if not isinstance(raw_time, str) or not raw_time:
        return None
    try:
        # 上游给的是 RFC3339（含 +08:00 或 Z）；fromisoformat 在 3.11+ 支持 Z。
        dt = datetime.datetime.fromisoformat(raw_time)
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # 本地时区，与 stats 的 day/hour 口径一致
    except (ValueError, TypeError, OverflowError):
        return None
    delta = item.get("delta")
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        return None
    return {
        "day": dt.strftime("%Y-%m-%d"),
        "hour": dt.hour,
        "uid": str(item.get("uid") or ""),
        "delta": delta,
    }
