"""
分钟级信号引擎
- 候选股：等待日内最优买入时机
- 持仓股：实时止损止盈监控
"""
from __future__ import annotations

import pandas as pd
from dataclasses import dataclass, field
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
    conditions: list = field(default_factory=list)
    # 每项格式：[label: str, ok: bool, detail: str]
    # label: 条件名称，ok: True=通过/未触发，False=不通过/触发，detail: 具体数值说明

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().strftime("%H:%M:%S")

    def is_action_required(self) -> bool:
        return self.action in (Action.BUY, Action.SELL_STOP, Action.SELL_PROFIT)


class IntradayEngine:

    # ── 候选股入场 ──────────────────────────────────

    def check_entry(self, code: str, daily_df: pd.DataFrame,
                    quote: dict, signal_type: str = "",
                    market_chg: float = 0.0) -> IntradaySignal:
        """
        日线已出买点信号 → 分钟级确认最优入场时机
        每一步检查结果都记录在 IntradaySignal.conditions 里
        """
        conds = []

        price = quote.get("price", 0)
        if not price:
            return IntradaySignal(Action.WAIT, 0, "报价异常", conditions=conds)

        # ── 1. 时间窗口 ──────────────────────────────
        _is_squeeze = "粘合" in signal_type or "发散" in signal_type
        _win = "9:45-11:30 / 13:00-14:30" if _is_squeeze else "10:00-11:30 / 13:30-14:30"
        if not is_buy_window(signal_type=signal_type):
            conds.append(["时间窗口", False, f"非买入窗口（{_win}）"])
            return IntradaySignal(Action.WAIT, price, f"非买入窗口（{_win}），等待", conditions=conds)
        conds.append(["时间窗口", True, f"在买入窗口 {_win} 内"])

        # ── 2. 跌停板 ────────────────────────────────
        last_close = quote.get("last_close", 0)
        if last_close > 0 and price <= last_close * 0.901:
            conds.append(["跌停板", False, f"价格{price:.2f}≤前收{last_close:.2f}×90.1%，无法成交"])
            return IntradaySignal(Action.WAIT, price, f"跌停板（前收{last_close:.2f}），无法买入", conditions=conds)
        conds.append(["跌停板", True, f"价格{price:.2f}，未跌停可成交"])

        # ── 3. 炸板（粘合发散专属）──────────────────
        high = quote.get("high", 0)
        low  = quote.get("low", 0)
        if _is_squeeze and last_close > 0 and high > 0:
            if high >= last_close * 1.095 and price < last_close * 1.095:
                conds.append(["炸板检测", False,
                    f"最高{(high/last_close-1)*100:.1f}%触及涨停区后回落至{(price/last_close-1)*100:.1f}%，突破失败"])
                return IntradaySignal(Action.WAIT, price,
                    f"炸板：曾触及涨停区（最高{(high/last_close-1)*100:.1f}%）后回落，突破失败",
                    conditions=conds)
            conds.append(["炸板检测", True,
                f"最高{(high/last_close-1)*100:.1f}%，未炸板"])

        # ── 4. 开盘缺口 ──────────────────────────────
        open_price = quote.get("open", 0)
        if open_price > 0 and last_close > 0:
            gap_pct     = (open_price - last_close) / last_close * 100
            _gap_thresh = -4.0 if "回踩" in signal_type else -3.0
            if gap_pct <= _gap_thresh:
                conds.append(["开盘缺口", False,
                    f"开盘跳空{gap_pct:.1f}%≤阈值{_gap_thresh:.0f}%，前日信号失效"])
                return IntradaySignal(Action.WAIT, price,
                    f"开盘跳空{gap_pct:.1f}%（阈值{_gap_thresh:.0f}%），前日信号失效，等待缺口修复",
                    conditions=conds)
            conds.append(["开盘缺口", True, f"开盘{gap_pct:+.1f}%，无异常缺口"])

        # ── 5. 当日涨跌幅 ────────────────────────────
        chg_pct = 0.0
        if last_close > 0:
            chg_pct = (price - last_close) / last_close * 100
            _chg_fail = ""
            if "粘合" in signal_type:
                if chg_pct >= 8.0:
                    _chg_fail = f"粘合发散当日涨幅{chg_pct:.1f}%≥8%，接近涨停不追"
                elif chg_pct <= -3.0:
                    _chg_fail = f"粘合发散当日跌{chg_pct:.1f}%，突破失败暂缓"
            elif "回踩" in signal_type:
                if chg_pct >= 5.0:
                    _chg_fail = f"回踩信号当日涨{chg_pct:.1f}%≥5%，已不在回踩区"
                elif chg_pct <= -5.0:
                    _chg_fail = f"回踩信号当日跌{chg_pct:.1f}%≤-5%，跌过头暂缓"
            else:
                if chg_pct >= 8.0:
                    _chg_fail = f"金叉信号当日涨幅{chg_pct:.1f}%≥8%，接近涨停不追"
                elif chg_pct <= -3.0:
                    _chg_fail = f"金叉信号当日跌{chg_pct:.1f}%≤-3%，弱势暂缓入场"
            if _chg_fail:
                conds.append(["当日涨跌幅", False, _chg_fail])
                return IntradaySignal(Action.WAIT, price, _chg_fail, conditions=conds)
            conds.append(["当日涨跌幅", True, f"当日{chg_pct:+.1f}%，在允许范围内"])

        # ── 6. 个股 vs 大盘相对强弱 ─────────────────
        if market_chg != 0 and last_close > 0:
            rs = chg_pct - market_chg
            _rs_thresh = -5.0 if "回踩" in signal_type else -3.0
            if rs <= _rs_thresh:
                conds.append(["相对强弱", False,
                    f"个股{chg_pct:+.1f}% - 大盘{market_chg:+.1f}% = RS{rs:+.1f}pp ≤ {_rs_thresh}pp，相对弱势"])
                return IntradaySignal(Action.WAIT, price,
                    f"相对大盘弱势{abs(rs):.1f}pp（个股{chg_pct:+.1f}% vs 大盘{market_chg:+.1f}%），等待强势",
                    conditions=conds)
            conds.append(["相对强弱", True,
                f"个股{chg_pct:+.1f}% - 大盘{market_chg:+.1f}% = RS{rs:+.1f}pp，强势可买"])

        # ── 7. 当日振幅 ──────────────────────────────
        if high > 0 and low > 0:
            amplitude = (high - low) / low * 100
            if amplitude >= 5.0:
                conds.append(["当日振幅", False, f"振幅{amplitude:.1f}%≥5%，筹码不稳，暂缓入场"])
                return IntradaySignal(Action.WAIT, price,
                    f"当日振幅{amplitude:.1f}%≥5%，筹码不稳，等待企稳", conditions=conds)
            conds.append(["当日振幅", True, f"振幅{amplitude:.1f}%<5%，筹码稳定"])

        # ── 8. 价格 vs MA20 ──────────────────────────
        last = daily_df.iloc[-1]
        ma20 = float(last["ma20"])
        ma5  = float(last["ma5"])

        if price < ma20:
            conds.append(["价格/MA20", False, f"价格{price:.2f} < MA20={ma20:.2f}，未站上均线"])
            return IntradaySignal(Action.WAIT, price,
                f"价格{price:.2f} < MA20={ma20:.2f}，不追", conditions=conds)
        conds.append(["价格/MA20", True, f"价格{price:.2f} > MA20={ma20:.2f}，站上均线"])

        # ── 9. 死叉保护（MA5）────────────────────────
        is_pullback = "回踩" in signal_type
        if not is_pullback:
            if price < ma5:
                conds.append(["死叉保护", False,
                    f"价格{price:.2f} < MA5={ma5:.2f}，短线趋势转弱，有死叉风险"])
                return IntradaySignal(Action.WAIT, price,
                    f"价格{price:.2f} < MA5={ma5:.2f}，短线趋势转弱，有死叉风险，等待企稳",
                    conditions=conds)
            conds.append(["死叉保护", True, f"价格{price:.2f} ≥ MA5={ma5:.2f}，无死叉风险"])
        else:
            conds.append(["死叉保护", True, f"回踩信号豁免MA5检查（合理区间 MA20~MA5）"])

        # ── 10. 追高保护 ─────────────────────────────
        deviation = (price / ma20 - 1) * 100
        if price > ma20 * 1.05:
            conds.append(["追高保护", False, f"偏离MA20 {deviation:.1f}%≥5%，追高风险"])
            return IntradaySignal(Action.WAIT, price,
                f"价格偏离MA20 {deviation:.1f}%，等回踩", conditions=conds)
        conds.append(["追高保护", True, f"偏离MA20 {deviation:.1f}%<5%，未追高"])

        # ── 11. 5分钟K站稳 + 趋势方向 + 量比 ────────
        min_df = get_minute_bars(code, freq="5m", count=20)
        recent_closes = []
        if min_df is not None and len(min_df) >= 3:
            recent_closes = min_df["close"].tail(3).tolist()
            stable      = all(c > ma20 for c in recent_closes)
            trending_up = recent_closes[-1] >= recent_closes[0]
            _rc_str     = " / ".join(f"{c:.2f}" for c in recent_closes)
            if stable:
                conds.append(["5分K站稳", True,  f"近3根({_rc_str})均在MA20={ma20:.2f}上方"])
            else:
                conds.append(["5分K站稳", False, f"近3根({_rc_str})有收盘低于MA20={ma20:.2f}"])
            if stable:
                if trending_up:
                    conds.append(["5分K方向", True,
                        f"最近{recent_closes[-1]:.2f}≥最早{recent_closes[0]:.2f}，趋势向上"])
                else:
                    conds.append(["5分K方向", False,
                        f"最近{recent_closes[-1]:.2f}<最早{recent_closes[0]:.2f}，分钟K下行"])
        else:
            stable = trending_up = True
            conds.append(["5分K站稳", True, "无分钟数据，退化为日线价格判断"])
            conds.append(["5分K方向", True, "无分钟数据，默认通过"])

        vol_ratio = quote.get("vol_ratio", 1.0) or 1.0
        vol_ok    = vol_ratio >= 1.0
        if vol_ok:
            conds.append(["量比", True,  f"量比{vol_ratio:.2f}≥1.0，量能达标"])
        else:
            conds.append(["量比", False, f"量比{vol_ratio:.2f}<1.0，量能不足"])

        if stable and trending_up and vol_ok:
            return IntradaySignal(
                Action.BUY, price,
                f"站稳MA20={ma20:.2f} 趋势向上 | 量比={vol_ratio:.2f}"
                f" | 个股{chg_pct:+.1f}% 大盘{market_chg:+.1f}%",
                urgency="urgent",
                conditions=conds,
            )

        reasons = []
        if not stable:      reasons.append("未站稳MA20")
        if not trending_up: reasons.append(
            f"分钟K下行({recent_closes[-1]:.2f}<{recent_closes[0]:.2f})" if recent_closes else "分钟K下行")
        if not vol_ok:      reasons.append(f"量比不足({vol_ratio:.2f})")
        return IntradaySignal(
            Action.WAIT, price,
            f"等待确认 | {' | '.join(reasons)}",
            conditions=conds,
        )

    # ── 持仓监控 ────────────────────────────────────

    @staticmethod
    def _exit_params(entry_signal: str) -> dict:
        """
        根据买入信号类型返回差异化的止盈参数：
          粘合发散：买在趋势最早期，给足空间让趋势跑起来
          金叉：  趋势已确认，标准参数
          回踩：  买在趋势后期，空间有限，快进快出
        """
        s = entry_signal or ""
        if "粘合" in s or "发散" in s:
            return {
                "dd_thresh":    8.0,   # 峰值回落容忍（pp），宽松
                "peak_lock_hi": 12.0,  # 峰值达到此值后开始锁利
                "peak_lock_lo": 12.0,  # 回落至此值触发止盈
            }
        if "回踩" in s:
            return {
                "dd_thresh":    3.0,   # 峰值回落容忍（pp），收紧
                "peak_lock_hi": 8.0,
                "peak_lock_lo": 8.0,
            }
        # 金叉 or 默认
        return {
            "dd_thresh":    5.0,
            "peak_lock_hi": 10.0,
            "peak_lock_lo": 10.0,
        }

    def check_position(self, code: str, daily_df: pd.DataFrame,
                       quote: dict, cost: float,
                       stop_price: float,
                       peak_pnl: float = 0.0,
                       entry_signal: str = "",
                       first_limit_up_date: str = "") -> IntradaySignal:
        """
        持仓实时止损止盈（每30秒调用）
        每一步检查结果都记录在 IntradaySignal.conditions 里
        """
        conds = []

        price = quote.get("price", 0)
        if not price:
            return IntradaySignal(Action.HOLD, 0, "报价异常", conditions=conds)

        last    = daily_df.iloc[-1]
        ma5     = float(last["ma5"])
        ma20    = float(last["ma20"])
        pnl_pct = (price - cost) / cost * 100

        # ① 硬止损
        if pnl_pct <= -5.0:
            conds.append(["①硬止损", False, f"盈亏{pnl_pct:.1f}%≤-5%，触发硬止损"])
            return IntradaySignal(Action.SELL_STOP, price,
                f"亏损{pnl_pct:.1f}%，触发止损（成本×95%）", urgency="urgent", conditions=conds)
        conds.append(["①硬止损", True, f"盈亏{pnl_pct:.1f}%>-5%，未触发"])

        # ② 趋势止损
        if price < ma20:
            min_df = get_minute_bars(code, freq="5m", count=10)
            below  = 0
            if min_df is not None and not min_df.empty:
                below = sum(1 for c in min_df["close"].tail(3) if c < ma20)
            if below >= 2:
                conds.append(["②趋势止损", False,
                    f"价格{price:.2f}<MA20={ma20:.2f}，连续{below}根5分钟K确认跌破，触发趋势止损"])
                return IntradaySignal(Action.SELL_STOP, price,
                    f"跌破MA20={ma20:.2f} 持续{below}根5分钟K | 盈亏{pnl_pct:.1f}%",
                    urgency="urgent", conditions=conds)
            conds.append(["②趋势止损", True,
                f"价格{price:.2f}<MA20={ma20:.2f}，但仅{below}根5分钟K，未达2根阈值"])
        else:
            conds.append(["②趋势止损", True, f"价格{price:.2f}≥MA20={ma20:.2f}，趋势完好"])

        # ③ 止损价
        if price < stop_price:
            conds.append(["③止损价", False, f"价格{price:.2f}<止损价{stop_price:.2f}，触发"])
            return IntradaySignal(Action.SELL_STOP, price,
                f"触及止损价{stop_price:.2f} | 盈亏{pnl_pct:.1f}%",
                urgency="urgent", conditions=conds)
        conds.append(["③止损价", True, f"价格{price:.2f}≥止损价{stop_price:.2f}，安全"])

        # ③' 接近涨停锁利
        chg_pct    = float(quote.get("change_pct", 0.0) or 0.0)
        _is_squeeze = "粘合" in entry_signal or "发散" in entry_signal
        if chg_pct >= 9.0 and pnl_pct > 0:
            if _is_squeeze:
                _today_str = datetime.now().strftime("%Y-%m-%d")
                if first_limit_up_date and first_limit_up_date < _today_str:
                    conds.append(["③'涨停锁利", False,
                        f"涨幅{chg_pct:.1f}%≥9%，粘合发散非首涨停日（首次={first_limit_up_date}），锁利"])
                    return IntradaySignal(Action.SELL_PROFIT, price,
                        f"当日涨幅{chg_pct:.1f}%，粘合发散非首涨停日主动锁利"
                        f"（首涨停={first_limit_up_date}）| 盈亏{pnl_pct:+.1f}%",
                        urgency="urgent", conditions=conds)
                conds.append(["③'涨停锁利", True,
                    f"涨幅{chg_pct:.1f}%≥9%，但粘合发散首涨停日（{first_limit_up_date or '今日'}），持有观察"])
            else:
                conds.append(["③'涨停锁利", False,
                    f"涨幅{chg_pct:.1f}%≥9%，金叉/回踩触发涨停锁利"])
                return IntradaySignal(Action.SELL_PROFIT, price,
                    f"当日涨幅{chg_pct:.1f}%接近涨停，主动锁利 | 盈亏{pnl_pct:+.1f}%",
                    urgency="urgent", conditions=conds)
        else:
            conds.append(["③'涨停锁利", True,
                f"涨幅{chg_pct:.1f}%<9%{'且盈亏≤0' if pnl_pct<=0 else ''}，未触发"])

        # 止盈时间窗口状态（上下文信息）
        _in_exit_win = is_profit_exit_window()
        conds.append(["止盈时间窗口", _in_exit_win,
            f"当前{'在' if _in_exit_win else '不在'} 14:30-15:00 止盈窗口"
            f"{'（止盈类信号可执行）' if _in_exit_win else '（止盈类信号等待）'}"])

        # ④ 日线死叉
        if ma5 < ma20 and pnl_pct > 0:
            if _in_exit_win:
                conds.append(["④日线死叉", False,
                    f"MA5={ma5:.2f}<MA20={ma20:.2f}，趋势结束，盈亏{pnl_pct:+.1f}%，窗口内止盈"])
                return IntradaySignal(Action.SELL_PROFIT, price,
                    f"日线死叉 MA5={ma5:.2f}<MA20={ma20:.2f}，趋势结束 | 盈亏{pnl_pct:+.1f}%",
                    urgency="urgent", conditions=conds)
            conds.append(["④日线死叉", False,
                f"MA5={ma5:.2f}<MA20={ma20:.2f}，死叉已触发，等14:30后执行"])
            return IntradaySignal(Action.HOLD, price,
                f"⏳死叉待执行(14:30后) | MA5={ma5:.2f}<MA20={ma20:.2f} | 盈亏{pnl_pct:+.1f}%",
                conditions=conds)
        conds.append(["④日线死叉", True, f"MA5={ma5:.2f}≥MA20={ma20:.2f}，无死叉，趋势完好"])

        # ④' RSI超买
        if "rsi14" in daily_df.columns and pnl_pct > 0 and peak_pnl >= 3.0:
            _rsi_recent = daily_df["rsi14"].dropna().tail(3).tolist()
            _rsi_ob_cnt = sum(1 for r in _rsi_recent if r > 80)
            _rsi_str    = " / ".join(f"{r:.0f}" for r in _rsi_recent)
            if _rsi_ob_cnt >= 2 and round(peak_pnl - pnl_pct, 2) >= 2.0:
                conds.append(["④'RSI超买", False,
                    f"近3日RSI({_rsi_str})中{_rsi_ob_cnt}日>80，峰值{peak_pnl:.1f}%→当前{pnl_pct:.1f}%，回落{peak_pnl-pnl_pct:.1f}pp≥2pp，动量见顶"])
                return IntradaySignal(Action.SELL_PROFIT, price,
                    f"RSI连续{_rsi_ob_cnt}日>80超买，动量见顶止盈 | "
                    f"峰值{peak_pnl:.1f}%→当前{pnl_pct:.1f}%（回落{peak_pnl-pnl_pct:.1f}pp≥2pp，超买阈值）",
                    urgency="urgent", conditions=conds)
            _why = f"RSI({_rsi_str})，{_rsi_ob_cnt}日>80"
            if _rsi_ob_cnt >= 2:
                _why += f"，但回落{peak_pnl-pnl_pct:.1f}pp<2pp，阈值未达"
            conds.append(["④'RSI超买", True, _why])
        else:
            conds.append(["④'RSI超买", True,
                f"RSI条件未满足（pnl={pnl_pct:.1f}% peak={peak_pnl:.1f}%）"])

        # ⑤ 浮盈回落保护
        ep   = self._exit_params(entry_signal)
        drop = round(peak_pnl - pnl_pct, 2)
        if peak_pnl >= 5.0 and drop >= ep["dd_thresh"] and pnl_pct > 0:
            if _in_exit_win:
                conds.append(["⑤浮盈回落", False,
                    f"峰值{peak_pnl:.1f}%回落{drop:.1f}pp≥{ep['dd_thresh']}pp，当前{pnl_pct:.1f}%，窗口内止盈"])
                return IntradaySignal(Action.SELL_PROFIT, price,
                    f"利润从峰值{peak_pnl:.1f}%回落{drop:.1f}pp至{pnl_pct:.1f}%，保护性止盈（容忍{ep['dd_thresh']}pp）",
                    urgency="urgent", conditions=conds)
            conds.append(["⑤浮盈回落", False,
                f"峰值{peak_pnl:.1f}%回落{drop:.1f}pp≥{ep['dd_thresh']}pp，等14:30后执行"])
            return IntradaySignal(Action.HOLD, price,
                f"⏳止盈待确认(14:30后执行) | 峰值{peak_pnl:.1f}%→当前{pnl_pct:.1f}%",
                conditions=conds)
        conds.append(["⑤浮盈回落", True,
            f"峰值{peak_pnl:.1f}%→当前{pnl_pct:.1f}%，回落{drop:.1f}pp<{ep['dd_thresh']}pp，保护未触发"])

        # ⑥ 峰值锁利
        if peak_pnl >= ep["peak_lock_hi"] and pnl_pct <= ep["peak_lock_lo"]:
            if _in_exit_win:
                conds.append(["⑥峰值锁利", False,
                    f"峰值{peak_pnl:.1f}%≥{ep['peak_lock_hi']}%，当前{pnl_pct:.1f}%≤{ep['peak_lock_lo']}%，窗口内锁利"])
                return IntradaySignal(Action.SELL_PROFIT, price,
                    f"利润从峰值{peak_pnl:.1f}%回落至{pnl_pct:.1f}%，锁定利润",
                    urgency="urgent", conditions=conds)
            conds.append(["⑥峰值锁利", False,
                f"峰值{peak_pnl:.1f}%≥{ep['peak_lock_hi']}%，当前{pnl_pct:.1f}%≤{ep['peak_lock_lo']}%，等14:30后执行"])
            return IntradaySignal(Action.HOLD, price,
                f"⏳止盈待确认(14:30后执行) | 峰值{peak_pnl:.1f}%→当前{pnl_pct:.1f}%",
                conditions=conds)
        conds.append(["⑥峰值锁利", True,
            f"峰值{peak_pnl:.1f}%，当前{pnl_pct:.1f}%，锁利条件（峰值≥{ep['peak_lock_hi']}%且回落至≤{ep['peak_lock_lo']}%）未达"])

        conds.append(["持仓状态", True,
            f"盈亏={pnl_pct:.1f}% | 峰值={peak_pnl:.1f}% | MA5={ma5:.2f} MA20={ma20:.2f}"])
        return IntradaySignal(
            Action.HOLD, price,
            f"持仓正常 | 盈亏={pnl_pct:.1f}% | 峰值={peak_pnl:.1f}% | MA5={ma5:.2f} MA20={ma20:.2f}",
            conditions=conds,
        )


# 全局单例
engine = IntradayEngine()
