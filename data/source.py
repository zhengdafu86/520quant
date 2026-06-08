"""
统一数据源抽象层
================================================
把"取数"收敛到一个接口，上层只调 get_source().get_minute/get_daily/get_fund，
不关心底层是谁。现在挂旧源（腾讯分钟 / mootdx日线 / 东财clist资金流），
等 QMT 就绪，塞入 XtDataSource 即无缝切换——且 xtdata 没有限流/封禁。

默认分钟粒度 = 1m（实盘目标）。

统一返回口径：
  get_minute / get_daily → DataFrame[datetime, open, high, low, close, vol]（时间升序）
  get_fund               → {code: (今日万, 5日万, 10日万)}，缺失为 None

用法:
  from data.source import get_source
  src = get_source()                       # 自动：有xtquant用XtData，否则旧源
  df  = src.get_minute("000001", "1m", count=240)
  fnd = src.get_fund(["000001", "600519"])

切换：环境变量 DATA_SOURCE=xtdata|legacy 可强制指定。
自检: python -m data.source
"""
from __future__ import annotations

import os
import pandas as pd

MINUTE_PERIOD = "1m"   # 默认分钟粒度（用户要求 1 分钟级）
_COLS = ["datetime", "open", "high", "low", "close", "vol"]


# ── 接口 ─────────────────────────────────────────────────
class DataSource:
    name = "base"

    def available(self) -> bool:
        return False

    def get_minute(self, code: str, period: str = MINUTE_PERIOD,
                   count: int = 320, start: str = "", end: str = "") -> pd.DataFrame:
        raise NotImplementedError

    def get_daily(self, code: str, bars: int = 320) -> pd.DataFrame:
        raise NotImplementedError

    def get_fund(self, codes) -> dict:
        raise NotImplementedError


# ── 旧源：腾讯分钟 / mootdx日线 / 东财clist资金流 ──────────
class LegacySource(DataSource):
    name = "legacy"

    def available(self) -> bool:
        return True

    def get_minute(self, code, period=MINUTE_PERIOD, count=320, start="", end=""):
        from data.fetcher import fetch_minute
        return fetch_minute(code, freq=period, count=count)

    def get_daily(self, code, bars=320):
        from data.fetcher import db
        df = db.get(code, freq="day", bars=bars)
        return df if df is not None else pd.DataFrame()

    def get_fund(self, codes):
        from scanner.ai_score import _fund_batch
        return _fund_batch(list(codes))


# ── 目标源：QMT xtdata（仅 Windows + QMT 终端在线可用）──────
class XtDataSource(DataSource):
    name = "xtdata"
    # xtquant 周期名映射
    _P = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m", "day": "1d"}

    def __init__(self):
        from xtquant import xtdata          # 仅在此处 import，缺库时 available()=False
        self._xt = xtdata

    def available(self) -> bool:
        return True

    @staticmethod
    def _norm_dt(t) -> str:
        """xtdata 时间('20260103093100' 或 毫秒时间戳) → 'YYYY-MM-DD HH:MM:SS'"""
        s = str(t)
        if len(s) >= 14 and s.isdigit():                      # YYYYMMDDHHMMSS
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
        if len(s) >= 8 and s[:8].isdigit() and not s[8:].strip():  # YYYYMMDD
            return f"{s[0:4]}-{s[4:6]}-{s[6:8]} 00:00:00"
        try:                                                  # 毫秒时间戳
            return pd.to_datetime(int(s), unit="ms").strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return s

    def _bars(self, code, period, count, start, end):
        xt = self._xt
        sec = self._secid(code)
        p = self._P.get(period, period)
        # 先下到本地缓存（首次/增量），再读取
        try:
            xt.download_history_data(sec, p, start or "", end or "")
        except Exception:
            pass
        data = xt.get_market_data_ex(
            ["time", "open", "high", "low", "close", "volume"],
            [sec], period=p, start_time=start or "", end_time=end or "",
            count=count if count else -1)
        df = data.get(sec)
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=_COLS)
        out = pd.DataFrame({
            "datetime": [self._norm_dt(x) for x in (df["time"] if "time" in df else df.index)],
            "open": df["open"].astype(float).values,
            "high": df["high"].astype(float).values,
            "low":  df["low"].astype(float).values,
            "close": df["close"].astype(float).values,
            "vol":  df["volume"].astype(float).values,
        })
        return out.sort_values("datetime").reset_index(drop=True)

    @staticmethod
    def _secid(code: str) -> str:
        return (code + ".SH") if code.startswith(("6", "9", "5")) else (code + ".SZ")

    def get_minute(self, code, period=MINUTE_PERIOD, count=320, start="", end=""):
        return self._bars(code, period, count, start, end)

    def get_daily(self, code, bars=320):
        return self._bars(code, "day", bars, "", "")

    def get_fund(self, codes):
        # xtdata 不直接给"主力净额"。暂沿用东财clist；
        # TODO(QMT就绪后)：用 xtdata 逐笔tick自算主力净流入，彻底摆脱东财限流。
        from scanner.ai_score import _fund_batch
        return _fund_batch(list(codes))


# ── 工厂：自动挑选 + 环境变量强制 ──────────────────────────
_CACHE = {}

def get_source(prefer: str = None) -> DataSource:
    """prefer/环境变量 DATA_SOURCE = 'xtdata' | 'legacy'；默认自动探测。"""
    choice = (prefer or os.environ.get("DATA_SOURCE", "")).lower()
    if choice in _CACHE:
        return _CACHE[choice]

    src = None
    if choice == "legacy":
        src = LegacySource()
    elif choice == "xtdata":
        src = XtDataSource()                 # 缺库会抛错，明确告知
    else:
        try:                                 # 自动：能 import xtquant 就用它
            src = XtDataSource()
        except Exception:
            src = LegacySource()

    _CACHE[choice] = src
    return src


if __name__ == "__main__":
    s = get_source()
    print(f"数据源: {s.name} | 默认分钟粒度: {MINUTE_PERIOD}")
    df = s.get_minute("000001", MINUTE_PERIOD, count=5)
    print("分钟K(尾部):")
    print(df.tail() if not df.empty else "  (空——旧源分钟可能受限/被封)")
