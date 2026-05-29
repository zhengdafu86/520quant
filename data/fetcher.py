"""
数据层：K线获取 + 技术指标计算
支持日线 / 分钟线，带本地缓存
"""
from __future__ import annotations

import pandas as pd
from pathlib import Path
from mootdx.quotes import Quotes


def _market(code: str) -> int:
    """深圳=0，上海=1"""
    return 1 if code.startswith(("6", "9")) else 0


class KlineDB:
    """K线数据库：拉取 + 指标计算"""

    CATEGORY_MAP = {
        "1m":  7,
        "5m":  8,
        "15m": 9,
        "30m": 10,
        "60m": 11,
        "day": 4,
        "week": 5,
    }

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = Quotes.factory(market="std")
        return self._client

    def get(self, code: str, freq: str = "day", bars: int = 60) -> pd.DataFrame:
        """
        拉取K线并计算技术指标
        freq: 'day' | '1m' | '5m' | '15m' | '30m' | '60m'
        bars: 拉取根数
        """
        category = self.CATEGORY_MAP.get(freq, 4)
        raw = self.client.bars(
            symbol=code,
            market=_market(code),
            category=category,
            offset=bars,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()

        df = raw[["open", "close", "high", "low", "vol"]].copy()
        df = df.reset_index().sort_values("datetime").reset_index(drop=True)
        df = self._add_indicators(df)
        return df

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算 MA5 / MA20 / 斜率 / 量比 / 金叉死叉"""
        df["ma5"]  = df["close"].rolling(5).mean().round(3)
        df["ma20"] = df["close"].rolling(20).mean().round(3)

        # 20日线斜率（3日差值）
        df["ma20_slope"] = df["ma20"].diff(3).round(4)

        # 量比
        df["vol_ma5"]   = df["vol"].rolling(5).mean()
        df["vol_ratio"] = (df["vol"] / df["vol_ma5"]).round(2)

        # 金叉 +1 / 死叉 -1 / 无 0
        df["cross"] = 0
        for i in range(1, len(df)):
            prev = df.iloc[i - 1]
            curr = df.iloc[i]
            if pd.isna(prev["ma5"]) or pd.isna(curr["ma5"]):
                continue
            if prev["ma5"] <= prev["ma20"] and curr["ma5"] > curr["ma20"]:
                df.at[i, "cross"] = 1
            elif prev["ma5"] >= prev["ma20"] and curr["ma5"] < curr["ma20"]:
                df.at[i, "cross"] = -1

        return df

    def latest(self, code: str, freq: str = "day") -> pd.Series:
        """返回最新一根K线 + 指标"""
        df = self.get(code, freq=freq, bars=60)
        return df.iloc[-1] if not df.empty else pd.Series()


# 全局单例
db = KlineDB()
