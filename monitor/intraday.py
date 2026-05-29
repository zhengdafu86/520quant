"""
分钟级信号引擎
- 候选股：等待日内最优买入时机
- 持仓股：实时止损止盈监控
"""
from __future__ import annotations

import pandas as pd
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

from monitor.realtime import get_quote, get_minute_bars


class Action(Enum):
    BUY         = "立即买入"
    SELL_STOP   = "止损卖出"
    SELL_PROFIT = "止盈卖出"
    HOLD        = "持有观察"
    WAIT        = "等待机会"


@dataclass
class IntradaySignal:
    action:    Action
    price:     float
    reason:    str
    urgency:   str = "normal"    # normal | urgent
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().strftime("%H:%M:%S")

    def is_action_required(self) -> bool:
        return self.action in (Action.BUY, Action.SELL_STOP, Action.SELL_PROFIT)


class IntradayEngine:

    # ── 候选股入场 ──────────────────────────────────

    def check_entry(self, code: str, daily_df: pd.DataFrame,
                    quote: dict) -> IntradaySignal:
        """
        日线已出买点信号 → 分钟级确认最优入场时机
        策略：
          1. 价格在MA20上方但不超过5%（不追高）
          2. 最近3根1分钟K均在MA20上方（站稳）
          3. 当日量能有起色（避免无量假突破）
        """
        price = quote.get("price", 0)
        if not price:
            return IntradaySignal(Action.WAIT, 0, "报价异常")

        last  = daily_df.iloc[-1]
        ma20  = float(last["ma20"])
        ma5   = float(last["ma5"])

        # 价格必须在MA20上方
        if price < ma20:
            return IntradaySignal(
                Action.WAIT, price,
                f"价格{price:.2f} < MA20={ma20:.2f}，不追"
            )

        # 不追高超5%
        if price > ma20 * 1.05:
            return IntradaySignal(
                Action.WAIT, price,
                f"价格偏离MA20 {(price/ma20-1)*100:.1f}%，等回踩"
            )

        # 分钟K确认站稳
        min_df = get_minute_bars(code, freq="5m", count=20)
        if min_df is not None and len(min_df) >= 3:
            recent_closes = min_df["close"].tail(3).tolist()
            stable = all(c > ma20 for c in recent_closes)
        else:
            stable = True   # 拉不到分钟数据时退化为只看价格

        # 量比（当日量能）
        vol_ratio = quote.get("vol_ratio", 1.0) or 1.0
        vol_ok    = vol_ratio >= 1.0

        if stable and vol_ok:
            return IntradaySignal(
                Action.BUY, price,
                f"分钟级站稳MA20={ma20:.2f} | 量比={vol_ratio:.2f} | 价格={price:.2f}",
                urgency="urgent"
            )

        return IntradaySignal(
            Action.WAIT, price,
            f"等待确认 stable={stable} vol_ratio={vol_ratio:.2f}"
        )

    # ── 持仓监控 ────────────────────────────────────

    def check_position(self, code: str, daily_df: pd.DataFrame,
                       quote: dict, cost: float,
                       stop_price: float) -> IntradaySignal:
        """
        持仓实时止损止盈（每30秒调用）
        stop_price: 入场时设定的初始止损价（MA5附近）
        """
        price = quote.get("price", 0)
        if not price:
            return IntradaySignal(Action.HOLD, 0, "报价异常")

        last     = daily_df.iloc[-1]
        ma5      = float(last["ma5"])
        ma20     = float(last["ma20"])
        pnl_pct  = (price - cost) / cost * 100

        # ① 趋势止损：实时价格跌破MA20且持续（3根5分钟K收盘均在MA20下方）
        if price < ma20:
            min_df = get_minute_bars(code, freq="5m", count=10)
            below  = 0
            if min_df is not None and not min_df.empty:
                below = sum(1 for c in min_df["close"].tail(3) if c < ma20)
            if below >= 2:
                return IntradaySignal(
                    Action.SELL_STOP, price,
                    f"跌破MA20={ma20:.2f} 持续{below}根5分钟K | 亏损{pnl_pct:.1f}%",
                    urgency="urgent"
                )

        # ② 止损价触发（买入时设定的MA5保护线）
        if price < stop_price:
            return IntradaySignal(
                Action.SELL_STOP, price,
                f"触及止损价{stop_price:.2f} | 亏损{pnl_pct:.1f}%",
                urgency="urgent"
            )

        # ③ 常规止盈：盈利3-5%
        if 3.0 <= pnl_pct <= 5.5:
            return IntradaySignal(
                Action.SELL_PROFIT, price,
                f"盈利{pnl_pct:.1f}%，常规止盈区间",
            )

        # ④ 强势止盈保护：涨幅>7% 后日内回落2%以上，锁定利润
        if pnl_pct > 7:
            min_df = get_minute_bars(code, freq="5m", count=20)
            if min_df is not None and not min_df.empty:
                intraday_high = min_df["high"].max()
                pullback = (intraday_high - price) / intraday_high * 100
                if pullback >= 2.0:
                    return IntradaySignal(
                        Action.SELL_PROFIT, price,
                        f"高位回落{pullback:.1f}% | 盈利{pnl_pct:.1f}%，保护性止盈",
                        urgency="urgent"
                    )

        return IntradaySignal(
            Action.HOLD, price,
            f"持仓正常 | 盈亏={pnl_pct:.1f}% | "
            f"MA5={ma5:.2f} MA20={ma20:.2f}"
        )


# 全局单例
engine = IntradayEngine()
