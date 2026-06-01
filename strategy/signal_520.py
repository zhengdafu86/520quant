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
    cross_date:  str   = ""      # 金叉形成日期 YYYY-MM-DD
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
        斜率阈值：MA20当前值的0.1%（动态，避免高低价股判断偏差）
        """
        slopes = df["ma20_slope"].dropna().tail(3).tolist()
        if len(slopes) < 3:
            return "flat"
        ma20_last = float(df["ma20"].dropna().iloc[-1])
        threshold = ma20_last * 0.001   # 0.1% 动态阈值
        pos = sum(1 for s in slopes if s > threshold)
        neg = sum(1 for s in slopes if s < -threshold)
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
        买点1：放量金叉（含3日内新鲜金叉）
        条件：
          - 20日线向上
          - 近3日内有MA5上穿MA20（金叉）
          - 金叉当日量比 >= 1.5
          - 当前收盘仍站上MA20
        """
        last = df.iloc[-1]
        if df["ma20_slope"].iloc[-1] <= 0:
            return None
        if last["close"] <= last["ma20"]:
            return None

        # 近5日内找金叉（优先最新），覆盖完整一个交易周
        recent5 = df.tail(5)
        cross_bar = None
        days_ago  = 0
        for i in range(len(recent5) - 1, -1, -1):
            if recent5.iloc[i]["cross"] == 1:
                cross_bar = recent5.iloc[i]
                days_ago  = len(recent5) - 1 - i
                break

        if cross_bar is None:
            return None
        if cross_bar["vol_ratio"] < 1.5:
            return None

        # 防追涨：当前收盘不能超过MA20的5%（刚金叉应贴近均线）
        if last["close"] > last["ma20"] * 1.05:
            return None

        score = 60
        if cross_bar["vol_ratio"] >= 2.0:
            score += 15
        if last["ma20_slope"] > 1.0:
            score += 10
        if last["close"] > last["ma5"]:
            score += 10
        score -= days_ago * 5   # 越新鲜越高分

        days_str = "今日" if days_ago == 0 else f"{days_ago}日前"
        vol_desc = (
            "成交量激增" if cross_bar["vol_ratio"] >= 3.0 else
            "明显放量"   if cross_bar["vol_ratio"] >= 2.0 else
            "温和放量"
        )

        try:
            cross_date = str(cross_bar["datetime"])[:10]
        except Exception:
            cross_date = ""

        reason = (
            f"📈 趋势：MA20连续向上，近期斜率{last['ma20_slope']:+.3f}\n"
            f"🔔 信号：{days_str}({cross_date})MA5({cross_bar['ma5']:.2f})上穿MA20({cross_bar['ma20']:.2f})，金叉成立\n"
            f"📊 量能：金叉当日量比{cross_bar['vol_ratio']}倍，{vol_desc}"
        )
        return SignalResult(
            signal=Signal.BUY_GOLDEN_CROSS,
            reason=reason,
            stop_price=round(float(last["ma5"]) * 0.97, 2),
            score=min(score, 100),
            cross_date=cross_date,
        )

    def check_pullback(self, df: pd.DataFrame) -> SignalResult | None:
        """
        买点2：缩量回踩MA20
        条件：
          - 20日线向上
          - 近期有过金叉
          - 股价回踩至MA20附近（±5%，A股弹性大适当放宽）
          - 缩量（量比 ≤ 0.9）或带量阳线重新站上MA5
        """
        if df["ma20_slope"].iloc[-1] <= 0:
            return None
        if self._last_golden_cross_idx(df) is None:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        ma20 = last["ma20"]
        ma5  = last["ma5"]

        # 回踩幅度：股价在MA20 ±5% 内（A股弹性大，±3%过严）
        near_ma20 = abs(last["close"] - ma20) / ma20 <= 0.05

        # 缩量（量比 ≤ 0.9，容纳A股常见的自然回踩量能）
        vol_shrink = last["vol_ratio"] <= 0.9

        # 带量阳线站上MA5
        bullish_reclaim = (
            last["close"] > ma5
            and last["close"] > prev["close"]
            and last["vol_ratio"] >= 1.0
        )

        # 跌破即止：盘中下影线跌破MA20，收盘收回MA20上方（强支撑信号）
        # 典型A股针形支撑：散户恐慌抛出，主力低吸，收盘拉回
        broke_and_recovered = (
            last["low"] < ma20               # 盘中跌破MA20
            and last["close"] >= ma20        # 收盘守住MA20
            and last["close"] > last["open"] # 收阳（多头主导）
        )

        # 跌破即止形态放宽 near_ma20 限制（收盘可能略高于MA20但仍算回踩区）
        if broke_and_recovered:
            near_ma20 = True

        if not near_ma20:
            return None
        if not (vol_shrink or bullish_reclaim or broke_and_recovered):
            return None

        score = 65
        if broke_and_recovered:
            score += 20   # 最强支撑形态，额外加分
        if vol_shrink and bullish_reclaim:
            score += 15
        if last["close"] > ma5:
            score += 10

        # 找前期金叉日期
        cross_date = ""
        try:
            cross_idx = self._last_golden_cross_idx(df)
            if cross_idx is not None:
                cross_date = str(df.loc[cross_idx]["datetime"])[:10]
        except Exception:
            pass

        dev_pct = abs(last["close"] - ma20) / ma20 * 100
        shadow_pct = round((ma20 - last["low"]) / ma20 * 100, 2) if broke_and_recovered else 0
        vol_desc = (
            f"下影线跌破MA20({ma20:.2f})后收回，跌破幅度{shadow_pct:.1f}%，"
            f"针形支撑强烈（量比{last['vol_ratio']}倍）" if broke_and_recovered else
            f"缩量回踩（量比{last['vol_ratio']}倍），浮筹已清洗" if vol_shrink else
            f"带量阳线重新站上MA5（量比{last['vol_ratio']}倍）"
        )
        cross_hint = f"（前期金叉 {cross_date}）" if cross_date else ""
        reason = (
            f"📈 趋势：MA20持续向上，均线支撑有效\n"
            f"🔔 信号：前期金叉{cross_hint}后回踩MA20({ma20:.2f})，偏离仅{dev_pct:.1f}%\n"
            f"📊 量能：{vol_desc}"
        )
        return SignalResult(
            signal=Signal.BUY_PULLBACK,
            reason=reason,
            stop_price=round(float(ma20) * 0.97, 2),
            score=min(score, 100),
            cross_date=cross_date,
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

        try:
            cross_date = str(last["datetime"])[:10]
        except Exception:
            cross_date = ""

        vol_desc = (
            "成交量暴增" if last["vol_ratio"] >= 3.0 else
            "大幅放量"   if last["vol_ratio"] >= 2.0 else
            "明显放量"
        )
        reason = (
            f"📈 趋势：MA20向上，MA5与MA20均线粘合{squeeze_days}日蓄势\n"
            f"🔔 信号：今日({cross_date})均线发散金叉，突破蓄势区间，爆发信号强烈\n"
            f"📊 量能：量比{last['vol_ratio']}倍，{vol_desc}"
        )
        return SignalResult(
            signal=Signal.BUY_SQUEEZE,
            reason=reason,
            stop_price=round(float(last["ma20"]), 2),
            score=min(score, 100),
            cross_date=cross_date,
        )

    # ── 震荡期判断 ──────────────────────────────────

    def is_oscillating(self, df: pd.DataFrame, window: int = 20) -> bool:
        """
        近N日均线频繁穿越（≥2次）→ 判定为震荡期，不进场
        MA5和MA20来回穿越说明趋势未确立，是明显的横盘震荡特征
        """
        if len(df) < window:
            return False
        cross_count = int((df.tail(window)["cross"] != 0).sum())
        return cross_count >= 2

    # ── 持仓止损止盈 ────────────────────────────────

    def check_exit(self, df: pd.DataFrame,
                   cost: float, hold_days: int = 0,
                   peak_pnl: float = 0.0) -> SignalResult | None:
        """
        持仓出场判断
        cost:     买入成本
        peak_pnl: 历史最高盈利%（由调用方实时更新）
        """
        import pandas as _pd

        last = df.iloc[-1]
        pnl  = (last["close"] - cost) / cost * 100

        # ① ATR动态止损：波动大 → 适度放宽，但绝对不超过 8% 亏损上限
        if "atr14" in last.index and not _pd.isna(last["atr14"]):
            atr_stop  = cost - 2.0 * float(last["atr14"])   # ATR动态止损价
            hard_stop = cost * 0.95   # 5% 基础止损（正常股用这条）
            cap_stop  = cost * 0.92   # 8% 绝对上限（高波动股兜底，防止ATR过大导致无限宽）
            # 逻辑：先取 ATR 和基础止损中较低者（宽松），再强制不低于 8% 上限
            dyn_stop  = max(min(atr_stop, hard_stop), cap_stop)
            if last["close"] < dyn_stop:
                return SignalResult(
                    signal=Signal.STOP_SHORT,
                    reason=(f"ATR动态止损 ATR={last['atr14']:.3f} | "
                            f"止损价={dyn_stop:.2f} | 亏损{pnl:.1f}%"),
                    score=98,
                )
        else:
            # 无ATR数据时回退到5%硬止损
            if pnl <= -5.0:
                return SignalResult(
                    signal=Signal.STOP_SHORT,
                    reason=f"亏损{pnl:.1f}%，触发止损（成本×95%）",
                    score=98,
                )

        # ② 趋势止损：放量跌破MA20
        if last["close"] < last["ma20"] and last["vol_ratio"] > 1.2:
            return SignalResult(
                signal=Signal.STOP_TREND,
                reason=(f"放量跌破MA20={last['ma20']:.2f} | "
                        f"量比={last['vol_ratio']} | 盈亏={pnl:.1f}%"),
                score=90,
            )

        # ② 时间止损：持仓≥20日且盈利未达5%，释放资金等待更好机会
        if hold_days >= 20 and pnl < 5.0:
            return SignalResult(
                signal=Signal.STOP_TREND,
                reason=(f"时间止损：持{hold_days}天盈利仅{pnl:.1f}%"
                        f"（目标5%未达到），释放资金"),
                score=70,
            )

        # ③ 浮盈回落保护：峰值≥5%，回落≥5pp，且当前仍盈利
        if peak_pnl >= 5.0 and (peak_pnl - pnl) >= 5.0 and pnl > 0:
            return SignalResult(
                signal=Signal.PROFIT_NORMAL,
                reason=(f"利润从峰值{peak_pnl:.1f}%回落{peak_pnl-pnl:.1f}pp"
                        f"至{pnl:.1f}%，保护性止盈"),
                score=80,
            )

        # ④ 回落保护止盈：峰值曾超过10%，现在回落至10%
        if peak_pnl >= 10.0 and pnl <= 10.0:
            return SignalResult(
                signal=Signal.PROFIT_NORMAL,
                reason=f"利润从峰值{peak_pnl:.1f}%回落至{pnl:.1f}%，锁定利润",
                score=85,
            )

        # ⑤ 死叉出场（趋势结束，无论盈亏都出）
        if last["cross"] == -1:
            if pnl > 0:
                return SignalResult(
                    signal=Signal.PROFIT_STRONG,
                    reason=f"死叉出现，趋势止盈 | 盈亏={pnl:+.1f}%",
                    score=90,
                )
            else:
                return SignalResult(
                    signal=Signal.STOP_TREND,
                    reason=f"死叉出现，趋势止损 | 盈亏={pnl:+.1f}%",
                    score=90,
                )

        return None

    # ── 主入口 ──────────────────────────────────────

    def analyze(self, df: pd.DataFrame,
                cost: float = None,
                hold_days: int = 0,
                peak_pnl: float = 0.0) -> SignalResult:
        """
        对外统一入口
        - 空仓模式：寻找买点
        - 持仓模式（传入cost）：判断是否止损止盈
        peak_pnl: 持仓期间历史最高盈利%，用于回落保护
        """
        if df.empty or len(df) < 25:
            return SignalResult(Signal.WATCH, "数据不足")

        direction = self.ma20_direction(df)

        # ── 第一步：20日线卡口 ──
        if direction == "down":
            return SignalResult(Signal.WATCH, "20日线向下，坚决不进场")
        if direction == "flat":
            return SignalResult(Signal.WATCH, "20日线走平，震荡观望")

        # ── 持仓模式：优先出场判断（持仓期间不过滤震荡，以防被套） ──
        if cost is not None:
            exit_sig = self.check_exit(df, cost, hold_days, peak_pnl)
            if exit_sig:
                return exit_sig
            last = df.iloc[-1]
            pnl  = (last["close"] - cost) / cost * 100
            return SignalResult(
                Signal.HOLD,
                f"趋势完好，持有 | 当前盈亏={pnl:.1f}% | 峰值={peak_pnl:.1f}% | "
                f"MA5={last['ma5']:.2f} MA20={last['ma20']:.2f}",
            )

        # ── 粘合发散优先检测（即使震荡期也放行，突破蓄势是强信号）──
        squeeze = self.check_squeeze_breakout(df)
        if squeeze:
            return squeeze

        # ── 空仓模式：震荡期过滤（粘合已放行，其余信号过滤）──
        if self.is_oscillating(df):
            return SignalResult(Signal.WATCH, "均线频繁穿越，震荡期不进场")

        # ── 找买点（回踩 > 金叉，粘合已在上方处理）──
        for fn in [self.check_pullback, self.check_golden_cross]:
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
