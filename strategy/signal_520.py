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
    signal:       Signal
    reason:       str
    stop_price:   float = 0.0
    score:        int   = 0       # 信号强度 0-100
    cross_date:   str   = ""      # 金叉形成日期 YYYY-MM-DD
    extra:        dict  = field(default_factory=dict)
    score_detail: list  = field(default_factory=list)   # [(delta, label), ...]


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

    def _ma20_turning_up(self, df: pd.DataFrame) -> bool:
        """
        金叉转头豁免：MA20 尚未正式转正，但正在快速转头向上。
        现实中金叉常发生在 MA20 刚收敛、还没翻正的拐点，硬卡"必须向上"会漏掉买点。
        全部满足才放行：
          1. 近2日内出现金叉（今日或昨日）
          2. 最近3日斜率连续改善（递增）
          3. 当前斜率虽为负，但绝对值 < MA20 的 0.3%（接近转正，非大幅下行）
          4. 收盘价站上 MA20
        """
        if len(df) < 5:
            return False
        last = df.iloc[-1]
        slopes = df["ma20_slope"].dropna().tail(3).tolist()
        if len(slopes) < 3:
            return False
        ma20_last = float(last["ma20"])
        if ma20_last <= 0:
            return False
        # 当前斜率必须落在 [-0.3% * MA20, +∞)：跌幅已收窄到接近转正
        if last["ma20_slope"] < -ma20_last * 0.003:
            return False
        # 斜率连续改善（严格递增）
        if not (slopes[0] < slopes[1] < slopes[2]):
            return False
        # 近2日内有金叉
        if not (df.tail(2)["cross"] == 1).any():
            return False
        # 收盘站上 MA20
        if last["close"] <= ma20_last:
            return False
        return True

    def _last_golden_cross_idx(self, df: pd.DataFrame, lookback: int = 30) -> int | None:
        """找最近一次金叉的行索引"""
        tail = df.tail(lookback)
        for i in range(len(tail) - 1, -1, -1):
            if tail.iloc[i]["cross"] == 1:
                return tail.index[i]
        return None

    # ── RSI + 换手率趋势评分（通用）─────────────────

    @staticmethod
    def _append_rsi_turnover_score(last, detail: list) -> int:
        """
        RSI(14) + 换手率趋势加减分，直接追加到 detail 列表，返回净分值。
        调用方须已在 detail 中写入基础分，本方法只追加增量。
        """
        delta = 0

        # RSI(14) — 动量判断
        rsi_val = last.get("rsi14")
        if rsi_val is not None and not pd.isna(rsi_val):
            rsi = float(rsi_val)
            if rsi < 40:
                detail.append((10, f"RSI{rsi:.0f}超卖"))
                delta += 10
            elif rsi < 50:
                detail.append((5, f"RSI{rsi:.0f}低位"))
                delta += 5
            elif rsi > 80:
                detail.append((-10, f"RSI{rsi:.0f}超买"))
                delta -= 10
            elif rsi > 70:
                detail.append((-5, f"RSI{rsi:.0f}偏高"))
                delta -= 5

        # 换手率趋势 — 主力建仓判断
        tt_val = last.get("turnover_trend5")
        if tt_val is not None and not pd.isna(tt_val):
            tt = int(tt_val)
            if tt >= 4:
                detail.append((10, f"换手放量{tt}日"))
                delta += 10
            elif tt >= 2:
                detail.append((5, f"换手趋势{tt}日"))
                delta += 5
            elif tt == 0:
                detail.append((-5, "换手无趋势"))
                delta -= 5

        return delta

    # ── 买点识别 ──────────────────────────────────────

    def check_golden_cross(self, df: pd.DataFrame, allow_turning: bool = False) -> SignalResult | None:
        """
        买点1：放量金叉（含3日内新鲜金叉）
        条件：
          - 20日线向上
          - 近5日内有MA5上穿MA20（金叉）
          - 金叉当日量比 >= 1.5
          - 当前收盘仍站上MA20
        allow_turning=True：转头豁免模式，MA20斜率虽未转正但正在快速向上，
                            跳过斜率硬卡口，并在评分上扣分体现拐点风险。
        """
        last = df.iloc[-1]
        if not allow_turning and df["ma20_slope"].iloc[-1] <= 0:
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

        # ── 评分：以 detail 列表累加，保证透明可追溯 ──
        detail: list = [(60, "金叉基础")]

        if cross_bar["vol_ratio"] >= 2.0:
            detail.append((15, f"放量{cross_bar['vol_ratio']:.1f}x"))

        # 斜率加分：用百分比斜率，避免高低价股不公平
        ma20_val = float(last["ma20"])
        is_turning = bool(last["ma20_slope"] <= 0)   # 转头豁免：斜率尚未转正
        if ma20_val > 0 and last["ma20_slope"] / ma20_val > 0.002:
            detail.append((10, "MA20斜率强劲"))
        elif is_turning:
            # 拐点风险：MA20尚未正式转正，扣分体现不确定性
            detail.append((-15, "MA20转头未确认"))

        if last["close"] > last["ma5"]:
            detail.append((10, "站上MA5"))

        if days_ago > 0:
            detail.append((-days_ago * 5, f"金叉{days_ago}日前"))

        # RSI + 换手率趋势
        self._append_rsi_turnover_score(last, detail)

        score = max(0, min(100, sum(d for d, _ in detail)))

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

        trend_desc = (
            f"MA20正在转头向上（斜率{last['ma20_slope']:+.3f}，跌幅快速收窄，拐点初现）"
            if is_turning else
            f"MA20连续向上，近期斜率{last['ma20_slope']:+.3f}"
        )
        reason = (
            f"📈 趋势：{trend_desc}\n"
            f"🔔 信号：{days_str}({cross_date})MA5({cross_bar['ma5']:.2f})上穿MA20({cross_bar['ma20']:.2f})，金叉成立\n"
            f"📊 量能：金叉当日量比{cross_bar['vol_ratio']}倍，{vol_desc}"
        )
        return SignalResult(
            signal=Signal.BUY_GOLDEN_CROSS,
            reason=reason,
            stop_price=round(float(last["ma5"]) * 0.97, 2),
            score=score,
            cross_date=cross_date,
            score_detail=detail,
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

        cross_idx = self._last_golden_cross_idx(df)   # 只调用一次
        if cross_idx is None:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        ma20 = last["ma20"]
        ma5  = last["ma5"]

        # ── 信号日收盘涨幅过大：回踩已结束，次日买入=追高 ──
        # 好的回踩日应温和反弹 +1%~+3%；若已涨超 5% 说明反弹行情走完，次日性价比差
        _sig_chg = float(
            (last["close"] - prev["close"]) / prev["close"] * 100
            if float(prev["close"]) > 0 else 0.0
        )
        if _sig_chg > 5.0:
            return None   # 信号日涨幅 > 5%，回踩买点失效

        # 回踩幅度：收紧到 ±3%（越贴近MA20，止损越紧、盈亏比越好）
        dev_ratio = abs(last["close"] - ma20) / ma20
        near_ma20 = dev_ratio <= 0.03

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
        # 量能上限：量比 > 2.5 的"巨量长下影"博弈剧烈/有对倒出货嫌疑，不算干净针形
        _HAMMER_VOL_CAP = 2.5
        broke_and_recovered = (
            last["low"] < ma20               # 盘中跌破MA20
            and last["close"] >= ma20        # 收盘守住MA20
            and last["close"] > last["open"] # 收阳（多头主导）
            and last["vol_ratio"] <= _HAMMER_VOL_CAP   # 量能上限，排除巨量长下影
        )

        # 跌破即止形态放宽 near_ma20 限制（收盘可能略高于MA20但仍算回踩区）
        if broke_and_recovered:
            near_ma20 = True

        if not near_ma20:
            return None
        # 质量收紧：必须「缩量回踩」或「针形支撑」，剔除"带量站回MA5"这类较弱形态
        if not (vol_shrink or broke_and_recovered):
            return None

        # ── 评分 ──────────────────────────────────────
        detail: list = [(65, "回踩基础")]

        if broke_and_recovered:
            detail.append((20, "针形支撑"))   # 最强支撑形态
        if vol_shrink and bullish_reclaim:
            detail.append((15, "缩量阳线站回MA5"))
        if last["close"] > ma5:
            detail.append((10, "站上MA5"))

        # 盈亏比：越贴近MA20，止损越紧、上方空间越大（回踩选股核心）
        if dev_ratio <= 0.01:
            detail.append((10, "贴近MA20·盈亏比佳"))
        elif dev_ratio <= 0.02:
            detail.append((5, "近MA20"))

        # 趋势强度：MA20斜率（百分比口径，避免高低价股偏差）
        if float(ma20) > 0 and last["ma20_slope"] / float(ma20) > 0.002:
            detail.append((8, "MA20斜率强劲"))

        # 时间衰减：前期金叉距今越远，回踩有效性越低（每5日扣1分，最多扣15）
        cross_date = ""
        try:
            loc = df.index.get_loc(cross_idx)
            days_since_cross = len(df) - loc - 1
            decay = min(days_since_cross // 5, 15)
            if decay > 0:
                detail.append((-decay, f"金叉{days_since_cross}天前"))
        except Exception:
            pass

        # RSI + 换手率趋势
        self._append_rsi_turnover_score(last, detail)

        score = max(0, min(100, sum(d for d, _ in detail)))

        # 前期金叉日期
        try:
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
            f"🔔 信号：前期金叉{cross_hint}后回踩MA20({ma20:.2f})，偏离仅{dev_pct:.1f}%"
            f"（信号日涨幅{_sig_chg:+.1f}%，温和反弹）\n"
            f"📊 量能：{vol_desc}"
        )
        return SignalResult(
            signal=Signal.BUY_PULLBACK,
            reason=reason,
            stop_price=round(float(ma20) * 0.97, 2),
            score=score,
            cross_date=cross_date,
            score_detail=detail,
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

        # ── 过滤①：收盘必须突破压缩区间高点 ──────────────
        # 只碰到高点但收盘回落是典型假突破，多方没有真正拿下防线
        recent_high = float(window["high"].max())
        if float(last["close"]) <= recent_high:
            return None

        # ── 过滤②：收盘实体强度，排除长上影线 ───────────
        # 收盘须在当日区间上半段，避免"摸高就跌"的胡子K线
        day_range = float(last["high"]) - float(last["low"])
        if day_range > 0 and (float(last["close"]) - float(last["low"])) / day_range < 0.5:
            return None

        # ── 评分 ──────────────────────────────────────
        detail: list = [(75, "粘合发散基础")]

        if last["vol_ratio"] >= 2.0:
            detail.append((10, f"放量{last['vol_ratio']:.1f}x"))
        if squeeze_days >= 7:
            detail.append((10, f"粘合{squeeze_days}日蓄势"))

        # RSI + 换手率趋势
        self._append_rsi_turnover_score(last, detail)

        score = max(0, min(100, sum(d for d, _ in detail)))

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
            f"🔔 信号：今日({cross_date})均线发散金叉，收盘突破压缩区高点{recent_high:.2f}，爆发信号强烈\n"
            f"📊 量能：量比{last['vol_ratio']}倍，{vol_desc}"
        )
        return SignalResult(
            signal=Signal.BUY_SQUEEZE,
            reason=reason,
            stop_price=round(float(last["ma20"]), 2),
            score=score,
            cross_date=cross_date,
            score_detail=detail,
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

    @staticmethod
    def _exit_params(entry_signal: str) -> dict:
        """根据买入信号类型返回差异化止盈参数（与 intraday._exit_params 保持一致）"""
        s = entry_signal or ""
        if "粘合" in s or "发散" in s:
            return {
                "time_gates":   [(60, 12.0), (45, 8.0), (30, 5.0)],  # 更宽松的时间门槛
                "dd_by_age":    [(60, 3.0), (30, 5.0), (0, 8.0)],    # 峰值回落容忍
                "peak_lock_hi": 12.0,
                "peak_lock_lo": 12.0,
            }
        if "回踩" in s:
            return {
                "time_gates":   [(50, 12.0), (30, 8.0), (15, 5.0)],  # 更严格的时间门槛
                "dd_by_age":    [(60, 1.5),  (30, 2.0), (0, 3.0)],
                "peak_lock_hi": 8.0,
                "peak_lock_lo": 8.0,
            }
        # 金叉 or 默认（维持原参数）
        return {
            "time_gates":   [(60, 12.0), (40, 8.0), (20, 5.0)],
            "dd_by_age":    [(60, 2.0),  (30, 3.0), (0, 5.0)],
            "peak_lock_hi": 10.0,
            "peak_lock_lo": 10.0,
        }

    def check_exit(self, df: pd.DataFrame,
                   cost: float, hold_days: int = 0,
                   peak_pnl: float = 0.0,
                   entry_signal: str = "") -> SignalResult | None:
        """
        持仓出场判断
        cost:         买入成本
        peak_pnl:     历史最高盈利%（由调用方实时更新）
        entry_signal: 买入触发信号，用于差异化止盈参数
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

        ep = self._exit_params(entry_signal)

        # ② 时间止损（分级门槛）：持仓越久要求收益越高，淘汰"温水煮蛙"
        # 粘合：30/45/60天 | 金叉：20/40/60天 | 回踩：15/30/50天
        for _days, _target in ep["time_gates"]:
            if hold_days >= _days and pnl < _target:
                return SignalResult(
                    signal=Signal.STOP_TREND,
                    reason=(f"时间止损：持{hold_days}天盈利仅{pnl:.1f}%"
                            f"（{_days}天目标{_target}%未达），释放资金"),
                    score=70,
                )

        # ③ RSI连续超买收紧止盈
        # 最近3根日K中≥2根RSI>80 → 动量处于顶点区，从峰值回落2pp即触发止盈
        # 原理：RSI>80说明短期涨幅透支，主力随时减仓；早出不踏空，比等到正常5-8pp回落更优
        # 阈值设为2pp：既能过滤普通日内震荡（<2pp），又能在回落刚开始时出场
        if "rsi14" in df.columns and pnl > 0 and peak_pnl >= 3.0:
            _rsi_recent = df["rsi14"].dropna().tail(3).tolist()
            _rsi_ob_cnt = sum(1 for r in _rsi_recent if r > 80)
            if _rsi_ob_cnt >= 2:
                _rsi_dd = 2.0   # 超买状态下只容忍2pp回落（远严于正常的3-8pp）
                if round(peak_pnl - pnl, 2) >= _rsi_dd:
                    return SignalResult(
                        signal=Signal.PROFIT_NORMAL,
                        reason=(
                            f"RSI连续{_rsi_ob_cnt}日>80超买，动量见顶收紧止盈 | "
                            f"峰值{peak_pnl:.1f}%→当前{pnl:.1f}%"
                            f"，回落{peak_pnl-pnl:.1f}pp≥{_rsi_dd}pp（超买阈值）"
                        ),
                        score=88,
                    )

        # ④ 浮盈回落保护（动态收紧）：持仓越久从峰值容忍的回落越小
        # 粘合：8/5/3pp | 金叉：5/3/2pp | 回踩：3/2/1.5pp
        if peak_pnl >= 5.0:
            dd_thresh = ep["dd_by_age"][2][1]   # 默认最宽松段（持仓最短）
            for _age, _thresh in ep["dd_by_age"]:
                if hold_days >= _age:
                    dd_thresh = _thresh
                    break
            if (peak_pnl - pnl) >= dd_thresh:
                sig = Signal.PROFIT_NORMAL if pnl > 0 else Signal.STOP_TREND
                return SignalResult(
                    signal=sig,
                    reason=(f"浮盈回落：峰值{peak_pnl:.1f}%→当前{pnl:.1f}%"
                            f"，回落{peak_pnl-pnl:.1f}pp≥{dd_thresh}pp"
                            f"（持仓{hold_days}天），{'止盈' if pnl>0 else '止损'}出场"),
                    score=80,
                )

        # ⑤ 峰值锁利：超过上限后回落至锁定线触发
        # 粘合12%→12% | 金叉10%→10% | 回踩8%→8%
        if peak_pnl >= ep["peak_lock_hi"] and pnl <= ep["peak_lock_lo"]:
            return SignalResult(
                signal=Signal.PROFIT_NORMAL,
                reason=f"利润从峰值{peak_pnl:.1f}%回落至{pnl:.1f}%，锁定利润",
                score=85,
            )

        # ⑥ 死叉出场（趋势结束，无论盈亏都出）
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
        # 金叉转头豁免：MA20尚未转正但正在快速转头向上时，放行金叉检测（仅空仓买点模式）
        if direction in ("down", "flat") and cost is None and self._ma20_turning_up(df):
            gc = self.check_golden_cross(df, allow_turning=True)
            if gc:
                return gc
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
