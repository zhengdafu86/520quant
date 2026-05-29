"""
实时行情层
- 腾讯API实时报价（30秒延迟，不限频）
- 交易时段判断
- 分钟K线（mootdx）
"""
from __future__ import annotations

import urllib.request
from datetime import datetime, time as dtime


# ── 交易时段 ──────────────────────────────────────────

TRADE_SESSIONS = [
    (dtime(9, 15), dtime(11, 30)),    # 含集合竞价
    (dtime(13, 0), dtime(15, 0)),
]

def is_trading_time(now: datetime = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(s <= t <= e for s, e in TRADE_SESSIONS)

def is_market_open(now: datetime = None) -> bool:
    """正式开盘（排除集合竞价）"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or \
           (dtime(13, 0)  <= t <= dtime(15, 0))


# ── 实时报价 ──────────────────────────────────────────

def _prefix(code: str) -> str:
    return "sh" if code.startswith(("6", "9")) else "sz"

def get_quotes(codes: list[str]) -> dict[str, dict]:
    """
    腾讯财经实时报价
    返回 {code: {name, price, open, high, low, vol, amount, change_pct, time}}
    """
    if not codes:
        return {}

    prefixed = [f"{_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")

    try:
        resp = urllib.request.urlopen(req, timeout=5)
        raw  = resp.read().decode("gbk")
    except Exception as e:
        print(f"[realtime] 行情获取失败: {e}")
        return {}

    result: dict[str, dict] = {}
    for line in raw.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        key  = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 50:
            continue
        code = key[2:]

        def v(idx, cast=float, default=0):
            try:
                return cast(vals[idx]) if vals[idx] else default
            except (ValueError, IndexError):
                return default

        result[code] = {
            "name":       vals[1],
            "price":      v(3),
            "last_close": v(4),
            "open":       v(5),
            "high":       v(33),
            "low":        v(34),
            "vol":        v(36, int),          # 手（100股）
            "amount_wan": v(37),               # 万元
            "change_pct": v(32),               # 涨跌幅%
            "change_amt": v(31),               # 涨跌额
            "turnover":   v(38),               # 换手率%
            "vol_ratio":  v(49),               # 量比
            "time":       vals[30] if len(vals) > 30 else "",
        }
    return result


def get_quote(code: str) -> dict:
    """单股报价"""
    result = get_quotes([code])
    return result.get(code, {})


# ── 分钟K线 ───────────────────────────────────────────

def get_minute_bars(code: str, freq: str = "1m", count: int = 60):
    """
    mootdx 分钟K线
    freq: '1m' / '5m' / '15m' / '30m' / '60m'
    """
    from data.fetcher import db
    return db.get(code, freq=freq, bars=count)
