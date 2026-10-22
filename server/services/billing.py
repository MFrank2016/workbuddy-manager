"""billing 的换算、区间校验与查询（迁移 Todo 17，契约 §3）。

这个模块只做**纯换算与查询**，不含 HTTP 细节：端点（`gateway.py`）与设置
（`settings.py`）都很薄，判定逻辑集中在这里，便于单测逐条钉住舍入与边界。

## 单位与舍入（契约里最容易做错的两处）

  · 积分（credit）是上游的扣费单位，本面板**没有**美元概念。换算只用
    `billing_credit_price_usd`（一个积分值多少美元）；
  · `billing_usd_rate`（美元→人民币汇率）**只**参与 `billing_currency=cny`
    时额外输出的 `codebuddy_balance_cny`，绝不参与任何 USD 字段——否则汇率一
    改，美元账单跟着变，客户端那边对不上历史；
  · `total_usage` 单位是**美分**，且等于**逐项** `round_half_up` 之后的和。
    先全局求和再舍入会得到不同的数（3×1.5 credit、price=0.03：逐项 4.5→5，
    和 15；全局 13.5→14），分项与总计对不上，客户端会显示「明细加起来不等于
    总额」。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .. import db

# 契约 §5 的三个 flag（默认值逐字取自该表；测试会与 settings_defaults 比对）。
DEFAULTS: dict[str, object] = {
    'billing_credit_price_usd': 0.03,
    'billing_usd_rate': 7.15,
    'billing_currency': 'usd',
}

CURRENCIES = ('usd', 'cny')

# 区间上限（天，含首尾）。契约：最大 365 天，默认最近 30 天。
MAX_RANGE_DAYS = 365
DEFAULT_RANGE_DAYS = 30

_DATE_FORMAT = '%Y-%m-%d'


class InvalidBilling(ValueError):
    """billing 设置的非法值（路由翻成 400，不改库）。"""


class InvalidRange(ValueError):
    """usage 区间的非法值（路由翻成 400 `invalid_date_range`）。"""


# ── 设置读取（非法存值一律回落默认，与 flag 的口径一致）──────────
def _price() -> float:
    value = db.get_setting('billing_credit_price_usd', DEFAULTS['billing_credit_price_usd'])
    return value if _is_positive_number(value) else float(DEFAULTS['billing_credit_price_usd'])


def _rate() -> float:
    value = db.get_setting('billing_usd_rate', DEFAULTS['billing_usd_rate'])
    return value if _is_positive_number(value) else float(DEFAULTS['billing_usd_rate'])


def _currency() -> str:
    value = db.get_setting('billing_currency', DEFAULTS['billing_currency'])
    return value if value in CURRENCIES else str(DEFAULTS['billing_currency'])


def settings_view() -> dict:
    """当前生效的三个设置（GET 的响应体，也是保存后的回显）。"""
    return {
        'billing_credit_price_usd': _price(),
        'billing_usd_rate': _rate(),
        'billing_currency': _currency(),
    }


def _is_positive_number(value: object) -> bool:
    # bool 是 int 的子类：`True` 不能被当成 1 块钱的单价。
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def merge(body: object) -> dict:
    """校验并返回**本次要写的**设置（部分更新；空对象 400）。

    只接受这三个键，且逐键严格校验：`'true'` / `1` / `-1` / `'CNY '` 一律拒绝。
    宽松解析会让前端与后端对「当前值」的理解分叉，而 billing 的数值直接变成
    账单，宁可 400 让人改。
    """
    if not isinstance(body, dict):
        raise InvalidBilling('请求体必须是对象')
    unknown = set(body) - set(DEFAULTS)
    if unknown:
        raise InvalidBilling(f'不认识的设置项：{", ".join(sorted(unknown))}')
    if not body:
        raise InvalidBilling('没有要保存的设置项')

    out: dict = {}
    if 'billing_credit_price_usd' in body:
        value = body['billing_credit_price_usd']
        if not _is_positive_number(value):
            raise InvalidBilling('billing_credit_price_usd 必须是大于 0 的数字')
        out['billing_credit_price_usd'] = float(value)
    if 'billing_usd_rate' in body:
        value = body['billing_usd_rate']
        if not _is_positive_number(value):
            raise InvalidBilling('billing_usd_rate 必须是大于 0 的数字')
        out['billing_usd_rate'] = float(value)
    if 'billing_currency' in body:
        value = body['billing_currency']
        # 不做 strip/lower：`'CNY '` 与 `'CNY'` 都不接受，避免「看起来存进去了、
        # 其实一直走默认」的那种静默失败。
        if not isinstance(value, str) or value not in CURRENCIES:
            raise InvalidBilling('billing_currency 只能是 usd 或 cny')
        out['billing_currency'] = value
    return out


# ── 舍入 ──────────────────────────────────────────────────
def round2(value: float) -> float:
    """四舍五入到两位小数（**half up**，不是 Python 的银行家舍入）。

    用 Decimal(str(...)) 而不是 `round()`：后者对二进制浮点是 half-even，
    `round(0.125, 2)` 给 0.12，而账单口径要 0.13。
    """
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def cents(credit: float, price: float) -> int:
    """`credit * price` 折算成**美分**，逐项 half-up 取整。"""
    amount = Decimal(str(credit)) * Decimal(str(price)) * 100
    return int(amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


# ── subscription ──────────────────────────────────────────
def subscription(key: dict) -> dict:
    """OpenAI 形状的订阅视图（契约 §3）。

    `quota_credit=0`（不限）时三个金额字段一律 **null**：客户端把 null 当
    「不限」，糊成 0 会让用户以为额度用完了。
    """
    price = _price()
    quota = float(key.get('quota_credit') or 0)
    used = float(key.get('used_credit') or 0)

    if quota > 0:
        hard = round2(quota * price)
        balance = round2(max(0.0, quota - used) * price)
    else:
        hard = None
        balance = None

    expires = key.get('expires_at')
    out = {
        'object': 'billing_subscription',
        'has_payment_method': False,
        'canceled': False,
        'canceled_at': None,
        'access_until': int(expires) if expires else None,
        'soft_limit_usd': 0,
        'hard_limit_usd': hard,
        'system_hard_limit_usd': hard,
        'codebuddy_balance_usd': balance,
    }
    # cny 只是**额外**字段：USD 三个字段在上面的计算里已经定稿，汇率不回头改它们。
    # 字段**在不在**只取决于币种设置；不限额度时值为 null（与 balance 同口径），
    # 客户端不必再判「这个键在不在」——形状在两种配额形态下保持一致。
    if _currency() == 'cny':
        out['codebuddy_balance_cny'] = (
            round2(balance * _rate()) if balance is not None else None)
    return out


# ── usage ─────────────────────────────────────────────────
def parse_range(start_date: str | None, end_date: str | None,
                today: date | None = None) -> tuple[str, str]:
    """解析并校验区间，返回 `(start_day, end_day)`（`YYYY-MM-DD`）。

    规则（契约 §3）：默认最近 30 天；`end >= start`；最长 365 天；非法 400。
    显式给了一端、另一端缺省时，缺的那端用「今天」/「今天往回 29 天」补齐，
    这样 `?start_date=X` 这种常见写法不会因为缺 end 就报错。
    """
    today = today or date.today()
    start = _parse_day(start_date, 'start_date')
    end = _parse_day(end_date, 'end_date') or today
    if start is None:
        start = end - timedelta(days=DEFAULT_RANGE_DAYS - 1)

    if end < start:
        raise InvalidRange('end_date 不能早于 start_date')
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise InvalidRange(f'区间最长 {MAX_RANGE_DAYS} 天')
    return start.isoformat(), end.isoformat()


def _parse_day(raw: str | None, field: str) -> date | None:
    if raw is None or raw == '':
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidRange(f'{field} 必须是 YYYY-MM-DD') from exc


def usage(key_id: int, start_day: str, end_day: str) -> dict:
    """按天、按模型的用量账单（契约 §3）。

    数据源是 `usage_daily`（与统计页同一份累计口径），只算**这把密钥自己**的
    行。credit 为 0 的行不产生 line_item —— 它们没有金额，列出来只会让客户端
    显示一堆 0.00 的行。
    """
    price = _price()
    rows = db.query(
        'SELECT day, model, COALESCE(SUM(credit), 0) AS credit FROM usage_daily '
        'WHERE key_id = ? AND day >= ? AND day <= ? '
        'GROUP BY day, model ORDER BY day ASC, model ASC',
        (key_id, start_day, end_day),
    )

    daily: dict[str, list[dict]] = {}
    total = 0
    for row in rows:
        cost = cents(float(row['credit'] or 0), price)
        if cost <= 0:
            continue
        daily.setdefault(str(row['day']), []).append({'name': row['model'], 'cost': cost})
        total += cost

    return {
        'object': 'list',
        'total_usage': total,
        'daily_costs': [
            {'timestamp': db.day_start_ts(day), 'line_items': items}
            for day, items in daily.items()
        ],
    }
