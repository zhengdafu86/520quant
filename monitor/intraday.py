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

from monitor.realtime import (get_quote, get_minute_bars,
                              is_buy_window, is_profit_exit_window)


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
                    quote: dict, signal_type: str = "") -> IntradaySignal:
        """
        日线已出买点信号 → 分钟级确认最优入场时机
        signal_type: WatchItem.signal，用于区分金叉/回踩/粘合，调整入场条件
        策略：
          1. 买入时间窗口：10:00-11:30 / 13:30-14:00
          2. 价格在MA20上方但不超过5%（不追高）
          3. 最近3根5分钟K均在MA20上方（站稳）
          4. 当日量能有起色（避免无量假突破）
          5. 非跌停板（跌停无法成交）
        """
        price = quote.get("price", 0)
        if not price:
            return IntradaySignal(Action.WAIT, 0, "报价异常")

        # ── 时间窗口过滤 ──────────────────────────────
        if not is_buy_window():
            return IntradaySignal(
                Action.WAIT, price,
                "非买入窗口（10:00-11:30 或 13:30-14:00），等待"
            )

        # ── 跌停板检测（无法成交）─────────────────────
        last_close = quote.get("last_close", 0)
        if last_close > 0 and price <= last_close * 0.901:
            return IntradaySignal(
                Action.WAIT, price,
                f"跌停板（前收{last_close:.2f}），无法买入"
            )

        last  = daily_df.iloc[-1]
        ma20  = float(last["ma20"])
        ma5   = float(last["ma5"])

        # 价格必须在MA20上方
        if price < ma20:
            return IntradaySignal(
                Action.WAIT, price,
                f"价格{price:.2f} < MA20={ma20:.2f}，不追"
            )

        # 死叉保护：金叉/粘合信号要求价格 ≥ MA5
        # 回踩信号的合理区间是 MA20 ≤ price ≤ MA5，不做此限制
        is_pullback = "回踩" in signal_type
        if not is_pullback and price < ma5:
            return IntradaySignal(
                Action.WAIT, price,
                f"价格{price:.2f} < MA5={ma5:.2f}，短线趋势转弱，有死叉风险，等待企稳"
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
                       stop_price: float,
                       peak_pnl: float = 0.0) -> IntradaySignal:
        """
        持仓实时止损止盈（每30秒调用）
        stop_price: 入场时设定的初始止损价
        peak_pnl:   持仓期间历史最高盈利%，用于回落保护
        """
        price = quote.get("price", 0)
        if not price:
            return IntradaySignal(Action.HOLD, 0, "报价异常")

        last    = daily_df.iloc[-1]
        ma5     = float(last["ma5"])
        ma20    = float(last["ma20"])
        pnl_pct = (price - cost) / cost * 100

        # ① 硬止损：亏损 >= 5%（跌至成本价×0.95）
        if pnl_pct <= -5.0:
            return IntradaySignal(
                Action.SELL_STOP, price,
                f"亏损{pnl_pct:.1f}%，触发止损（成本×95%）",
                urgency="urgent"
            )

        # ② 趋势止损：跌破MA20持续3根5分钟K
        if price < ma20:
            min_df = get_minute_bars(code, freq="5m", count=10)
            below  = 0
            if min_df is not None and not min_df.empty:
                below = sum(1 for c in min_df["close"].tail(3) if c < ma20)
            if below >= 2:
                return IntradaySignal(
                    Action.SELL_STOP, price,
                    f"跌破MA20={ma20:.2f} 持续{below}根5分钟K | 盈亏{pnl_pct:.1f}%",
                    urgency="urgent"
                )

        # ③ 止损价触发
        if price < stop_price:
            return IntradaySignal(
                Action.SELL_STOP, price,
                f"触及止损价{stop_price:.2f} | 盈亏{pnl_pct:.1f}%",
                urgency="urgent"
            )

        # ── 止盈类信号：14:30 前先观察，14:30 后才执行 ──────────
        _in_exit_win = is_profit_exit_window()

        # ④ 日线死叉（MA5 下穿 MA20）：趋势结束，且当前仍盈利 → 主动止盈
        # 这是金叉入场的对称出场信号；亏损持仓不触发（止损逻辑已覆盖）
        if ma5 < ma20 and pnl_pct > 0:
            if _in_exit_win:
                return IntradaySignal(
                    Action.SELL_PROFIT, price,
                    f"日线死叉 MA5={ma5:.2f}<MA20={ma20:.2f}，趋势结束 | 盈亏{pnl_pct:+.1f}%",
                    urgency="urgent"
                )
            return IntradaySignal(
                Action.HOLD, price,
                f"⏳死叉待执行(14:30后) | MA5={ma5:.2f}<MA20={ma20:.2f} | 盈亏{pnl_pct:+.1f}%"
            )

        # ⑤ 浮盈回落保护：峰值≥5%，回落≥5pp，且当前仍盈利
        if peak_pnl >= 5.0 and (peak_pnl - pnl_pct) >= 5.0 and pnl_pct > 0:
            if _in_exit_win:
                return IntradaySignal(
                    Action.SELL_PROFIT, price,
                    f"利润从峰值{peak_pnl:.1f}%回落{peak_pnl-pnl_pct:.1f}pp"
                    f"至{pnl_pct:.1f}%，保护性止盈",
                    urgency="urgent"
                )
            return IntradaySignal(
                Action.HOLD, price,
                f"⏳止盈待确认(14:30后执行) | 峰值{peak_pnl:.1f}%→当前{pnl_pct:.1f}%"
            )

        # ⑥ 回落保护止盈：峰值曾超过10%，现回落至10%
        if peak_pnl >= 10.0 and pnl_pct <= 10.0:
            if _in_exit_win:
                return IntradaySignal(
                    Action.SELL_PROFIT, price,
                    f"利润从峰值{peak_pnl:.1f}%回落至{pnl_pct:.1f}%，锁定利润",
                    urgency="urgent"
                )
            return IntradaySignal(
                Action.HOLD, price,
                f"⏳止盈待确认(14:30后执行) | 峰值{peak_pnl:.1f}%→当前{pnl_pct:.1f}%"
            )

        return IntradaySignal(
            Action.HOLD, price,
            f"持仓正常 | 盈亏={pnl_pct:.1f}% | 峰值={peak_pnl:.1f}% | "
            f"MA5={ma5:.2f} MA20={ma20:.2f}"
        )


# 全局单例
engine = IntradayEngine()
