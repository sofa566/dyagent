"""
示範技能：價格換算

Handler 簽名：convert(payload: dict) -> dict

輸入 payload 範例：
{
  "amount": 100,
  "from": "USD",
  "to": "TWD",
  "rate": 31.2   # 可選，若未提供則用內建近似匯率估值
}

回傳範例：
{
  "ok": true,
  "from": {"currency": "USD", "amount": 100.0},
  "to": {"currency": "TWD", "amount": 3120.0},
  "rate": 31.2,
  "ts": "2026-03-18T12:00:00Z",
  "note": "示範用，非即時匯率"
}
"""
from __future__ import annotations

from datetime import datetime, timezone


def convert(payload: dict) -> dict:
    try:
        amount = float(_get(payload, 'amount', 0))
        cur_from = str(_get(payload, 'from', 'USD')).upper()
        cur_to = str(_get(payload, 'to', 'USD')).upper()
        # 可選自訂匯率
        rate_opt = _get(payload, 'rate', None)
        if rate_opt is not None:
            rate = float(rate_opt)
        else:
            # 近似匯率（以 USD 為基準），示範用，非即時資料
            usd = {
                'USD': 1.0,
                'EUR': 0.92,
                'JPY': 151.0,
                'TWD': 32.0,
                'CNY': 7.2,
                'HKD': 7.8,
            }
            if cur_from not in usd or cur_to not in usd:
                return _err(f"unsupported currency: {cur_from}->{cur_to}")
            # from->USD->to
            rate = (usd[cur_to] / usd[cur_from])
        result = amount * rate
        return {
            'ok': True,
            'from': {'currency': cur_from, 'amount': amount},
            'to': {'currency': cur_to, 'amount': round(result, 4)},
            'rate': round(rate, 6),
            'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'note': '示範用，非即時匯率',
        }
    except Exception as e:
        return _err(str(e))


def _get(d: dict, key: str, default=None):
    try:
        return d.get(key, default)
    except Exception:
        return default


def _err(msg: str) -> dict:
    return {'ok': False, 'error': str(msg)}
