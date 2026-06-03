"""
数据层：K线获取 + 技术指标计算
支持日线 / 分钟线，带本地缓存
"""
from __future__ import annotations

import threading
import datetime as _dt
import pandas as pd
from pathlib import Path
from mootdx.quotes import Quotes


def _market(code: str) -> int:
    """深圳=0，上海=1（5开头的ETF如510300归上海）"""
    return 1 if code.startswith(("6", "9", "5")) else 0


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
        self._lock   = threading.Lock()   # mootdx TCP 连接非线程安全，必须序列化

    # 备用服务器列表（直接连接，不依赖 mootdx 配置文件）
    _SERVERS = [
        ("110.41.147.114", 7709),   # 深圳双线1
        ("124.70.176.52",  7709),   # 上海双线1
        ("121.36.54.217",  7709),   # 北京双线1
        ("124.71.85.110",  7709),   # 广州双线1
    ]

    @property
    def client(self):
        if self._client is None:
            for ip, port in self._SERVERS:
                try:
                    self._client = Quotes.factory(
                        market="std", ip=ip, port=port
                    )
                    break
                except Exception:
                    continue
        return self._client

    def get(self, code: str, freq: str = "day", bars: int = 60) -> pd.DataFrame:
        """
        拉取K线并计算技术指标
        freq: 'day' | '1m' | '5m' | '15m' | '30m' | '60m'
        bars: 拉取根数（实际返回根数，已去掉盘中未完成 K 线）

        日线特殊处理：
          mootdx 在交易时段会把今天的实时价格作为最后一根日线推入，
          该 K 线未完成（收盘价为当前价、成交量仅为盘中累计），
          会导致 MA / 量比等指标偏差，进而产生误判信号。
          解决方案：多取一根，若最后一根是今日盘中数据则丢弃，
          确保策略始终基于最近一次完整收盘数据运行。
        """
        category = self.CATEGORY_MAP.get(freq, 4)
        # 日线多取一根，丢弃今日盘中 K 线后仍能满足 bars 根需求
        fetch_bars = bars + 1 if freq == "day" else bars

        with self._lock:   # 序列化 mootdx 调用，防止多线程竞争
            raw = self.client.bars(
                symbol=code,
                market=_market(code),
                category=category,
                offset=fetch_bars,
            )
        if raw is None or raw.empty:
            return pd.DataFrame()

        df = raw[["open", "close", "high", "low", "vol"]].copy()
        df = df.reset_index().sort_values("datetime").reset_index(drop=True)

        # 日线：收盘前（15:00 前）若最后一根 K 线是今日盘中数据，丢弃它
        if freq == "day" and not df.empty:
            last_dt = pd.to_datetime(df.iloc[-1]["datetime"])
            now = _dt.datetime.now()
            if last_dt.date() == now.date() and now.time() < _dt.time(15, 0):
                df = df.iloc[:-1].reset_index(drop=True)

        # 截取到请求的根数（丢弃最旧的多余 K 线）
        df = df.tail(bars).reset_index(drop=True)
        df = self._add_indicators(df)
        return df

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算 MA5 / MA20 / MA60 / 斜率 / 量比 / 金叉死叉 / ATR14 / 成交额"""
        df["ma5"]  = df["close"].rolling(5).mean().round(3)
        df["ma20"] = df["close"].rolling(20).mean().round(3)
        df["ma60"] = df["close"].rolling(60).mean().round(3)   # 大盘趋势过滤用

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

        # ATR14：动态止损用
        df["tr"] = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"]  - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        df["atr14"] = df["tr"].rolling(14).mean().round(3)

        # 日成交额（万元）和5日均值：流动性过滤用
        df["turnover"]     = (df["close"] * df["vol"] * 100 / 10_000).round(0)   # 万元
        df["avg_turnover"] = df["turnover"].rolling(5).mean().round(0)            # 5日均（万元）

        # RSI(14) — Wilder 指数平滑（EWM alpha=1/14），超卖/超买判断
        _delta     = df["close"].diff()
        _gain      = _delta.clip(lower=0)
        _loss      = (-_delta).clip(lower=0)
        _avg_gain  = _gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        _avg_loss  = _loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        _rs        = _avg_gain / _avg_loss.where(_avg_loss != 0, other=float("nan"))
        df["rsi14"] = (100 - 100 / (1 + _rs)).round(1)

        # 换手率趋势：最近5日中"价涨 + 换手放大"的天数（0-5）
        # ≥3 → 主力持续建仓信号；0 → 量价背离，市场萎缩
        _up_exp = (
            (df["close"]   > df["close"].shift(1)) &
            (df["turnover"] > df["turnover"].shift(1))
        ).astype(int)
        df["turnover_trend5"] = _up_exp.rolling(5).sum().fillna(0).astype(int)

        return df

    def latest(self, code: str, freq: str = "day") -> pd.Series:
        """返回最新一根K线 + 指标"""
        df = self.get(code, freq=freq, bars=60)
        return df.iloc[-1] if not df.empty else pd.Series()

    def get_market(self, freq: str = "day", bars: int = 60) -> pd.DataFrame:
        """获取沪深300 ETF（510300）作为大盘代理，用于趋势过滤"""
        return self.get("510300", freq=freq, bars=bars)

    def get_ma20_direction(self, code: str, bars: int = 60) -> str:
        """
        计算某代码的 MA20 方向，主要用于行业 ETF 趋势判断。
        返回: 'up' | 'down' | 'flat' | 'unknown'
        阈值与主策略 ma20_direction() 保持一致（0.1% 百分比斜率）。
        """
        df = self.get(code, freq="day", bars=bars)
        if df.empty or len(df) < 3:
            return "unknown"
        last     = df.iloc[-1]
        ma20_val = float(last.get("ma20")       or 0)
        slope    = float(last.get("ma20_slope") or 0)
        if ma20_val <= 0:
            return "unknown"
        threshold = ma20_val * 0.001    # 0.1%，与信号策略一致
        if slope > threshold:
            return "up"
        if slope < -threshold:
            return "down"
        return "flat"

    def get_20d_return(self, code: str) -> float:
        """
        计算代码的 20 个交易日价格涨幅（%），用于相对强度（RS）基准计算。
        数据不足时返回 0.0。
        """
        df = self.get(code, freq="day", bars=30)
        if df.empty or len(df) < 21:
            return 0.0
        c_now = float(df.iloc[-1]["close"])
        c_20d = float(df.iloc[-21]["close"])
        if c_20d <= 0:
            return 0.0
        return round((c_now - c_20d) / c_20d * 100, 2)


# 全局单例
db = KlineDB()
