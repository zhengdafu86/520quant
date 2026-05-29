"""
策略层：520战法信号引擎
- 第一步：判断20日线方向
- 第二步：识别三种买点
- 第三步：止损止盈
"""
from __future__ import annotations

import pandas as pd
from dataclasses import dataclass, field
from enum import Enum


class Signal(Enum):
    BUY_GOLDEN_CROSS = "金叉买点"
    BUY_PULLBACK     = "回踩买点"
    BUY_SQUEEZE      = "粘合发散买点"
    HOLD             = "持有"
    STOP_SHORT       = "短线止损"
    STOP_TREND       = "趋势止损"
    PROFIT_NORMAL    = "常规止盈"
    PROFIT_STRONG    = "强势止盈(死叉)"
    WATCH            = "观望"

    def is_buy(self):
        return self in (Signal.BUY_GOLDEN_CROSS,
                        Signal.BUY_PULLBACK,
                        Signal.BUY_SQUEEZE)

    def is_exit(self):
        return self in (Signal.STOP_SHORT, Signal.STOP_TREND,
                        Signal.PROFIT_NORMAL, Signal.PROFIT_STRONG)


@dataclass
class SignalResult:
    signal:      Signal
    reason:      str
    stop_price:  float = 0.0
    score:       int   = 0       # 信号强度 0-100
    extra:       dict  = field(default_factory=dict)


class Strategy520:
    """
    520战法核心策略
    只有两条均线：5日 + 20日
    """

    # ── 工具 ─────────────────────────────────────────

    def ma20_direction(self, df: pd.DataFrame) -> str:
        """
        判断20日线方向
        返回 'up' / 'flat' / 'down'
        """
        slopes = df["ma20_slope"].dropna().tail(3).tolist()
        if len(slopes) < 3:
            return "flat"
        pos = sum(1 for s in slopes if s > 0.02)
        neg = sum(1 for s in slopes if s < -0.02)
        if pos >= 2:
            return "up"
        if neg >= 2:
            return "down"
        return "flat"

    def _last_golden_cross_idx(self, df: pd.DataFrame, lookback: int = 30) -> int | None:
        """找最近一次金叉的行索引"""
        tail = df.tail(lookback)
        for i in range(len(tail) - 1, -1, -1):
            if tail.iloc[i]["cross"] == 1:
                return tail.index[i]
        return None

    # ── 买点识别 ──────────────────────────────────────

    def check_golden_cross(self, df: pd.DataFrame) -> SignalResult | None:
        """
        买点1：放量金叉
        条件：
          - 20日线向上
          - 今日MA5上穿MA20（金叉）
          - 量比 >= 1.5
          - 股价站上MA20
        """
        last = df.iloc[-1]
        if last["cross"] != 1:
            return None
        if df["ma20_slope"].iloc[-1] <= 0:
            return None
        if last["vol_ratio"] < 1.5:
            return None
        if last["close"] <= last["ma20"]:
            return None

        score = 60
        if last["vol_ratio"] >= 2.0:
            score += 15
        if last["ma20_slope"] > 1.0:
            score += 10
        if last["close"] > last["ma5"]:
            score += 10

        return SignalResult(
            signal=Signal.BUY_GOLDEN_CROSS,
            reason=(f"金叉 | MA5={last['ma5']} 上穿 MA20={last['ma20']} | "
                    f"量比={last['vol_ratio']} | MA20斜率={last['ma20_slope']:+.3f}"),
            stop_price=round(float(last["ma5"]) * 0.97, 2),
            score=min(score, 100),
        )

    def check_pullback(self, df: pd.DataFrame) -> SignalResult | None:
        """
        买点2：缩量回踩MA20
        条件：
          - 20日线向上
          - 近期有过金叉
          - 股价回踩至MA20附近（±3%）
          - 缩量（量比 < 0.7）或带量阳线重新站上MA5
        """
        if df["ma20_slope"].iloc[-1] <= 0:
            return None
        if self._last_golden_cross_idx(df) is None:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        ma20 = last["ma20"]
        ma5  = last["ma5"]

        # 回踩幅度：股价在MA20 ±3% 内
        near_ma20 = abs(last["close"] - ma20) / ma20 <= 0.03

        # 缩量50-70%
        vol_shrink = last["vol_ratio"] <= 0.7

        # 带量阳线站上MA5
        bullish_reclaim = (
            last["close"] > ma5
            and last["close"] > prev["close"]
            and last["vol_ratio"] >= 1.0
        )

        if not near_ma20:
            return None
        if not (vol_shrink or bullish_reclaim):
            return None

        score = 65
        if vol_shrink and bullish_reclaim:
            score += 15
        if last["close"] > ma5:
            score += 10

        return SignalResult(
            signal=Signal.BUY_PULLBACK,
            reason=(f"回踩MA20 | 收盘={last['close']} MA20={ma20:.3f} | "
                    f"量比={last['vol_ratio']} | {'缩量' if vol_shrink else ''}{'带量反弹' if bullish_reclaim else ''}"),
            stop_price=round(float(ma20) * 0.97, 2),
            score=min(score, 100),
        )

    def check_squeeze_breakout(self, df: pd.DataFrame) -> SignalResult | None:
        """
        买点3：均线粘合发散
        条件：
          - 前5日 MA5-MA20 差值绝对值 < MA20的1%（粘合）
          - 今日金叉 + 放量（量比>=1.5）+ MA20向上
          - 股价突破近期震荡高点
        """
        if len(df) < 26:
            return None

        last = df.iloc[-1]
        if last["cross"] != 1:
            return None
        if last["vol_ratio"] < 1.5:
            return None
        if df["ma20_slope"].iloc[-1] <= 0:
            return None

        # 检查前5日粘合
        window = df.iloc[-7:-1]    # 排除今天，看之前6天取5天有效
        valid  = window.dropna(subset=["ma5", "ma20"])
        if len(valid) < 5:
            return None

        squeeze_days = sum(
            1 for _, r in valid.iterrows()
            if abs(r["ma5"] - r["ma20"]) / r["ma20"] < 0.01
        )
        if squeeze_days < 5:
            return None

        score = 75
        if last["vol_ratio"] >= 2.0:
            score += 10
        if squeeze_days >= 7:
            score += 10

        return SignalResult(
            signal=Signal.BUY_SQUEEZE,
            reason=(f"粘合{squeeze_days}日后发散 | 放量金叉 量比={last['vol_ratio']} | "
                    f"MA20斜率={last['ma20_slope']:+.3f}"),
            stop_price=round(float(last["ma20"]), 2),
            score=min(score, 100),
        )

    # ── 持仓止损止盈 ────────────────────────────────

    def check_exit(self, df: pd.DataFrame,
                   cost: float, hold_days: int = 0) -> SignalResult | None:
        """
        持仓出场判断
        cost:      买入成本
        hold_days: 已持有天数（0=当天）
        """
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        pnl  = (last["close"] - cost) / cost * 100

        # ① 趋势止损：放量跌破MA20 收盘收不回
        trend_break = (
            last["close"] < last["ma20"]
            and last["vol_ratio"] > 1.2
        )
        if trend_break:
            return SignalResult(
                signal=Signal.STOP_TREND,
                reason=(f"放量跌破MA20={last['ma20']:.2f} | "
                        f"量比={last['vol_ratio']} | 亏损={pnl:.1f}%"),
                score=95,
            )

        # ② 短线止损：收盘跌破MA5，且非当天（给一天确认）
        short_break = last["close"] < last["ma5"]
        if short_break and hold_days >= 1:
            return SignalResult(
                signal=Signal.STOP_SHORT,
                reason=(f"跌破MA5={last['ma5']:.2f} | "
                        f"收盘={last['close']} | 亏损={pnl:.1f}%"),
                score=80,
            )

        # ③ 常规止盈：盈利3-5%
        if 3.0 <= pnl <= 5.5:
            return SignalResult(
                signal=Signal.PROFIT_NORMAL,
                reason=f"盈利{pnl:.1f}%，进入常规止盈区间",
                score=70,
            )

        # ④ 强势止盈：出现死叉
        if last["cross"] == -1:
            return SignalResult(
                signal=Signal.PROFIT_STRONG,
                reason=f"死叉出现，强势止盈 | 盈亏={pnl:.1f}%",
                score=90,
            )

        return None

    # ── 主入口 ──────────────────────────────────────

    def analyze(self, df: pd.DataFrame,
                cost: float = None,
                hold_days: int = 0) -> SignalResult:
        """
        对外统一入口
        - 空仓模式：寻找买点
        - 持仓模式（传入cost）：判断是否止损止盈
        """
        if df.empty or len(df) < 25:
            return SignalResult(Signal.WATCH, "数据不足")

        direction = self.ma20_direction(df)

        # ── 第一步：20日线卡口 ──
        if direction == "down":
            return SignalResult(Signal.WATCH, "20日线向下，坚决不进场")
        if direction == "flat":
            return SignalResult(Signal.WATCH, "20日线走平，震荡观望")

        # ── 持仓模式：优先出场判断 ──
        if cost is not None:
            exit_sig = self.check_exit(df, cost, hold_days)
            if exit_sig:
                return exit_sig
            last = df.iloc[-1]
            pnl  = (last["close"] - cost) / cost * 100
            return SignalResult(
                Signal.HOLD,
                f"趋势完好，持有 | 当前盈亏={pnl:.1f}% | "
                f"MA5={last['ma5']:.2f} MA20={last['ma20']:.2f}",
            )

        # ── 空仓模式：找买点（优先级 粘合>回踩>金叉）──
        for fn in [self.check_squeeze_breakout,
                   self.check_pullback,
                   self.check_golden_cross]:
            result = fn(df)
            if result:
                return result

        last = df.iloc[-1]
        return SignalResult(
            Signal.WATCH,
            f"20日线向上但无明确买点 | "
            f"MA5={last['ma5']:.2f} MA20={last['ma20']:.2f} | "
            f"斜率={last['ma20_slope']:+.3f}",
        )


# 全局单例
strategy = Strategy520()
