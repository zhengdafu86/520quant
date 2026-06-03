"""
回测引擎：520战法历史回测
- 股票池：自选股（paper DB watchlist）
- 时间段：近 N 根日线（默认 250 ≈ 1年）
- 最多 4 仓同时持有，每仓 1/4 总资金
- 无前视偏差：信号在 T 日收盘后识别，T+1 日开盘价成交
- 大盘过滤：沪深300 ETF（510300）MA20 > MA60 才允许新建仓（指数自身金叉，与个股策略同逻辑）
- 流动性过滤：5日均成交额 < 3000万 的个股不买
"""
from __future__ import annotations

import sys
import pandas as pd
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from data.fetcher import db
from strategy.signal_520 import strategy, Signal


COMMISSION = 0.0003   # 万3 佣金（买卖双向）
STAMP_TAX  = 0.001    # 千1 印花税（仅卖出）

MIN_TURNOVER = 3_000   # 最低流动性：5日均成交额 3000万元（单位：万元）


# ── 数据结构 ──────────────────────────────────────────

@dataclass
class Trade:
    code:        str
    name:        str
    signal_date: str          # 信号触发日（T日）
    buy_date:    str          # 实际成交日（T+1日）
    sell_date:   str   = ""
    buy_price:   float = 0.0
    sell_price:  float = 0.0
    shares:      int   = 0
    pnl:         float = 0.0
    pnl_pct:     float = 0.0
    hold_days:   int   = 0
    signal:      str   = ""
    exit_signal: str   = ""


# ── 回测引擎 ──────────────────────────────────────────

class Backtester:

    def __init__(self, init_capital: float = 200_000, max_positions: int = 4):
        self.init_capital  = init_capital
        self.max_positions = max_positions

    def run(self, stocks: list[dict], bars: int = 250,
            start_date: str = "", end_date: str = "",
            priority_map: dict[str, str] | None = None):
        """
        stocks       : [{"code": "600487", "name": "亨通光电"}, ...]
        bars         : 回测天数（日线根数），start_date 优先
        start_date   : 回测开始日期，格式 "2025-06-01"（优先于 bars）
        end_date     : 回测结束日期，默认今天
        priority_map : {code: "P1"/"P2"/"P3"}，手动 WATCHLIST 优先级，仓位有限时先占坑
        """
        priority_map = priority_map or {}
        _PKEY = {"P1": 1, "P2": 2, "P3": 3}
        pos_size = self.init_capital / self.max_positions   # 每仓资金

        print(f"\n{'='*65}")
        print(f"  520战法  历史回测")
        print(f"  股票数: {len(stocks)} 只  |  初始资金: {self.init_capital:,.0f} 元")
        print(f"  最大持仓: {self.max_positions} 只  |  每仓: {pos_size:,.0f} 元")
        print(f"  买入规则: T日信号 → T+1日开盘价成交")
        print(f"  大盘过滤: 沪深300 ETF MA20 > MA60（指数金叉）才开新仓")
        print(f"  流动性门槛: 5日均成交额 ≥ {MIN_TURNOVER:,} 万元")
        if priority_map:
            names = {s["code"]: s["name"] for s in stocks}
            for level in ["P1", "P2", "P3"]:
                codes_at = [c for c, p in priority_map.items() if p == level and c in names]
                if codes_at:
                    pri_str = " | ".join(f"{names[c]}({c})" for c in codes_at)
                    print(f"  ⭐ {level}: {pri_str}")
        print(f"{'='*65}\n")

        # ── 计算实际需要拉取的 bars（必须在拉数据前计算）──
        # 若指定了 start_date，按日期区间估算所需根数，避免数据截断
        fetch_bars = bars + 60   # 默认：回测天数 + 指标预热
        if start_date:
            from datetime import date as _date
            try:
                s_dt  = _date.fromisoformat(start_date)
                today = _date.today()
                # 关键：bars 要从 start_date 到【今天】计算
                # db.get(bars=N) 返回的是最新 N 根，必须覆盖到 start_date
                days_to_today = (today - s_dt).days
                needed = int(days_to_today * 0.71 * 1.1) + 60
                fetch_bars = max(fetch_bars, needed)
            except ValueError:
                fetch_bars = max(fetch_bars, 1500)
            print(f"指定区间 {start_date} ~ {end_date or '最新'}，"
                  f"将拉取最近 {fetch_bars} 根日线以覆盖完整区间\n")

        # ── 拉取大盘数据（510300 沪深300 ETF）──
        print("拉取大盘数据（510300 沪深300 ETF）...")
        market_df = db.get_market(bars=fetch_bars)
        if market_df.empty:
            print("  ⚠️ 大盘数据获取失败，将跳过大盘过滤")
            market_df = None
            market_date_to_pos: dict[str, int] = {}
        else:
            market_df = market_df.reset_index(drop=True)
            market_df["date_str"] = market_df["datetime"].astype(str).str[:10]
            market_date_to_pos = {
                row["date_str"]: i for i, row in market_df.iterrows()
            }
            print(f"  ✓ 大盘数据: {len(market_df)} 根\n")

        # ── 拉取历史日线数据 ──
        print("拉取个股日线数据...")
        stock_data: dict[str, dict] = {}

        for s in stocks:
            code = s["code"]
            # 多拉 60 根作为指标预热（MA20=20根，ATR14=14根，留余量）
            df = db.get(code, freq="day", bars=fetch_bars)
            if df.empty:
                print(f"  ✗  {s['name']}({code})  无数据")
                continue
            df = df.reset_index(drop=True)
            df["date_str"] = df["datetime"].astype(str).str[:10]
            date_to_pos = {row["date_str"]: i for i, row in df.iterrows()}
            stock_data[code] = {
                "name":        s["name"],
                "df":          df,
                "date_to_pos": date_to_pos,
            }
            print(f"  ✓  {s['name']}({code}):  {len(df)} 根")

        if not stock_data:
            print("无可用数据，回测终止")
            return

        # ── 构建回测时间轴 ──
        all_dates = sorted({d for data in stock_data.values()
                             for d in data["date_to_pos"]})

        if start_date:
            sim_dates = [d for d in all_dates
                         if d >= start_date and (not end_date or d <= end_date)]
        else:
            sim_dates = all_dates[-bars:]

        if not sim_dates:
            print(f"指定区间 {start_date} ~ {end_date} 内无数据")
            return

        print(f"\n回测区间: {sim_dates[0]} ~ {sim_dates[-1]}  ({len(sim_dates)} 个交易日)\n")

        # ── 逐日模拟 ──
        cash                 = float(self.init_capital)
        positions            : dict[str, dict] = {}   # code -> 持仓信息
        pending_buys         : dict[str, dict] = {}   # code -> 待成交（T+1 开盘执行）
        trades               : list[Trade]     = []
        equity_log           : list[tuple]     = []
        market_blocked_days  : int             = 0    # 大盘过滤屏蔽建仓天数
        day_close_prev       : dict[str, float] = {}  # 前一日收盘价（用于T+1开盘涨跌幅过滤）

        for date_str in sim_dates:

            # 当日各股开盘/收盘价
            day_open:  dict[str, float] = {}
            day_close: dict[str, float] = {}
            for code, data in stock_data.items():
                pi = data["date_to_pos"].get(date_str)
                if pi is not None:
                    row = data["df"].iloc[pi]
                    day_open[code]  = float(row["open"])
                    day_close[code] = float(row["close"])

            # ── 1. 执行昨日待成交（T+1 开盘价）──
            for code, pb in list(pending_buys.items()):
                # 持仓已满或该股已有仓位（前一个tick的卖出释放的仓位可复用）
                if code in positions or len(positions) >= self.max_positions:
                    del pending_buys[code]
                    continue
                data = stock_data.get(code)
                if data is None:
                    del pending_buys[code]
                    continue
                price = day_open.get(code, 0.0)
                if price <= 0:
                    # 当日无开盘价（停牌），推迟到下一交易日
                    continue

                # 涨跌幅过滤：T+1开盘相对前收涨跌幅
                prev_close = day_close_prev.get(code, 0)
                if prev_close > 0:
                    gap_pct = (price - prev_close) / prev_close * 100
                    sig_type = pb.get("signal", "")
                    is_breakout = "粘合" in sig_type
                    is_pullback = "回踩" in sig_type
                    if is_breakout and gap_pct >= 8.0:
                        del pending_buys[code]; continue
                    elif is_pullback and (gap_pct >= 5.0 or gap_pct <= -5.0):
                        del pending_buys[code]; continue
                    elif not is_breakout and not is_pullback and (gap_pct >= 8.0 or gap_pct <= -3.0):
                        del pending_buys[code]; continue

                shares = int(pos_size / price / 100) * 100
                if shares < 100:
                    del pending_buys[code]
                    continue
                buy_amt = price * shares
                fee     = round(buy_amt * COMMISSION, 2)
                if cash < buy_amt + fee:
                    del pending_buys[code]
                    continue

                cash -= buy_amt + fee
                positions[code] = {
                    "name":        pb["name"],
                    "buy_price":   price,
                    "shares":      shares,
                    "signal_date": pb["signal_date"],
                    "buy_date":    date_str,
                    "hold_days":   0,
                    "signal":      pb["signal"],
                    "peak_pnl":    0.0,
                }
                del pending_buys[code]

            # ── 2. 检查持仓出场（收盘价）──
            for code in list(positions.keys()):
                if code not in stock_data:
                    continue
                data = stock_data[code]
                pi   = data["date_to_pos"].get(date_str)
                if pi is None or pi < 24:
                    continue

                sub_df = data["df"].iloc[: pi + 1]
                pos    = positions[code]
                pos["hold_days"] += 1

                cur_pnl = (day_close.get(code, pos["buy_price"]) - pos["buy_price"]) \
                          / pos["buy_price"] * 100
                pos["peak_pnl"] = max(pos.get("peak_pnl", 0.0), cur_pnl)

                result = strategy.analyze(
                    sub_df,
                    cost=pos["buy_price"],
                    hold_days=pos["hold_days"],
                    peak_pnl=pos["peak_pnl"],
                )

                if result.signal.is_exit():
                    price   = day_close.get(code, pos["buy_price"])
                    shares  = pos["shares"]
                    gross   = price * shares
                    fee     = round(gross * (COMMISSION + STAMP_TAX), 2)
                    net     = gross - fee
                    buy_amt = pos["buy_price"] * shares
                    pnl     = round(net - buy_amt, 2)
                    pnl_pct = round(pnl / buy_amt * 100, 2)

                    trades.append(Trade(
                        code=code, name=pos["name"],
                        signal_date=pos["signal_date"],
                        buy_date=pos["buy_date"], sell_date=date_str,
                        buy_price=pos["buy_price"], sell_price=price,
                        shares=shares, pnl=pnl, pnl_pct=pnl_pct,
                        hold_days=pos["hold_days"],
                        signal=pos["signal"],
                        exit_signal=result.signal.value,
                    ))
                    cash += net
                    del positions[code]

            # ── 3. 扫描买点 → 加入 pending（T+1 开盘成交）──
            # 先检查大盘：收盘价 > MA60 才允许新开仓
            # MA60 ≈ 季度趋势，比 MA20 方向更稳定，能有效屏蔽熊市假信号
            market_up = True
            mpi = None
            if market_df is not None:
                mpi = market_date_to_pos.get(date_str)
                if mpi is not None and mpi >= 59:
                    mrow   = market_df.iloc[mpi]
                    m_ma20 = mrow.get("ma20", float("nan"))
                    m_ma60 = mrow.get("ma60", float("nan"))
                    if (not pd.isna(m_ma20) and not pd.isna(m_ma60)
                            and float(m_ma60) > 0):
                        # 大盘金叉：MA20 > MA60（与个股520战法同逻辑）
                        market_up = float(m_ma20) > float(m_ma60)
                    else:
                        market_up = True   # 数据异常时放行
                elif mpi is not None and mpi >= 24:
                    # MA60 数据不够时降级用 MA20 方向（仅回测初期）
                    mdf = market_df.iloc[: mpi + 1]
                    market_up = strategy.ma20_direction(mdf) == "up"

            if not market_up:
                market_blocked_days += 1

            if market_up:
                # ── 计算大盘20日收益率（RS排序基准）──
                market_20d_ret = 0.0
                if market_df is not None and mpi is not None and mpi >= 20:
                    mc_now = float(market_df.iloc[mpi]["close"])
                    mc_20d = float(market_df.iloc[mpi - 20]["close"])
                    if mc_20d > 0:
                        market_20d_ret = (mc_now - mc_20d) / mc_20d * 100

                # ── 计算各候选股的相对强度 RS ──────────
                rs_scores: dict[str, float] = {}
                for c, d in stock_data.items():
                    pi2 = d["date_to_pos"].get(date_str)
                    if pi2 is None or pi2 < 20:
                        continue
                    c_now = float(d["df"].iloc[pi2]["close"])
                    c_20d = float(d["df"].iloc[pi2 - 20]["close"])
                    if c_20d > 0:
                        rs_scores[c] = round(
                            (c_now - c_20d) / c_20d * 100 - market_20d_ret, 2
                        )

                # P1→P2→P3→无优先级；同优先级内 RS 高的先占坑
                scan_order = sorted(
                    stock_data.keys(),
                    key=lambda c: (
                        _PKEY.get(priority_map.get(c, ""), 99),
                        -rs_scores.get(c, 0.0),    # RS 降序（强势股优先）
                    )
                )
                for code in scan_order:
                    # 持仓 + 待成交 不超过上限
                    if (code in positions or code in pending_buys
                            or len(positions) + len(pending_buys) >= self.max_positions):
                        continue
                    # ↓ 关键修复：必须用 stock_data[code]，不能用外层循环残留的 `data`
                    data = stock_data[code]
                    pi = data["date_to_pos"].get(date_str)
                    if pi is None or pi < 24:
                        continue

                    sub_df = data["df"].iloc[: pi + 1]

                    # 流动性过滤：5日均成交额 < 3000万 不买
                    last_row = sub_df.iloc[-1]
                    if "avg_turnover" in last_row.index and not pd.isna(last_row["avg_turnover"]):
                        if float(last_row["avg_turnover"]) < MIN_TURNOVER:
                            continue

                    result = strategy.analyze(sub_df)
                    if not result.signal.is_buy():
                        continue

                    # 信号确认：加入 pending，T+1 开盘执行
                    pending_buys[code] = {
                        "name":        data["name"],
                        "signal":      result.signal.value,
                        "signal_date": date_str,
                        "rs_score":    rs_scores.get(code, 0.0),
                    }

            # ── 4. 记录当日净值 ──
            pos_val = sum(
                day_close.get(c, p["buy_price"]) * p["shares"]
                for c, p in positions.items()
            )
            equity_log.append((date_str, round(cash + pos_val, 2)))

            # 更新前收价（供下一日T+1涨跌幅过滤使用）
            day_close_prev = dict(day_close)

        # ── 计算基准（沪深300 ETF）同期收益 ──
        benchmark_ret = 0.0
        if market_df is not None and sim_dates:
            m_start = market_date_to_pos.get(sim_dates[0])
            m_end   = market_date_to_pos.get(sim_dates[-1])
            if m_start is None:
                # 找最近的大盘日期
                for d in sim_dates:
                    if d in market_date_to_pos:
                        m_start = market_date_to_pos[d]
                        break
            if m_end is None:
                for d in reversed(sim_dates):
                    if d in market_date_to_pos:
                        m_end = market_date_to_pos[d]
                        break
            if m_start is not None and m_end is not None and m_start != m_end:
                c0 = float(market_df.iloc[m_start]["close"])
                c1 = float(market_df.iloc[m_end]["close"])
                if c0 > 0:
                    benchmark_ret = (c1 - c0) / c0 * 100

        # ── 输出报告 ──
        self._print_report(trades, equity_log, positions, day_close,
                           benchmark_ret=benchmark_ret,
                           trading_days=len(sim_dates),
                           market_blocked_days=market_blocked_days)

    # ── 报告 ──────────────────────────────────────────

    def _print_report(self, trades: list[Trade], equity_log: list[tuple],
                      open_pos: dict, last_prices: dict,
                      benchmark_ret: float = 0.0,
                      trading_days: int = 0,
                      market_blocked_days: int = 0):
        import math

        print(f"\n{'='*65}")
        print(f"  回测结果报告")
        print(f"{'='*65}")

        # ── 账户概览 ──────────────────────────────────
        annual_ret = 0.0
        max_dd     = 0.0
        calmar     = 0.0
        sharpe     = 0.0
        if not trading_days:
            trading_days = len(equity_log)

        if equity_log:
            final_eq  = equity_log[-1][1]
            total_ret = (final_eq - self.init_capital) / self.init_capital * 100

            # 年化收益（按 252 个交易日折算）
            if trading_days > 0:
                annual_ret = total_ret * 252 / trading_days

            # 最大回撤 & 回撤持续最长天数
            peak      = self.init_capital
            dd_start  = 0
            max_dd_days = 0
            cur_dd_days = 0
            for i, (_, eq) in enumerate(equity_log):
                if eq >= peak:
                    peak = eq
                    cur_dd_days = 0
                    dd_start = i
                else:
                    cur_dd_days += 1
                    max_dd_days = max(max_dd_days, cur_dd_days)
                max_dd = max(max_dd, (peak - eq) / peak * 100)

            # 卡玛比率 = 年化收益 / 最大回撤
            calmar = annual_ret / max_dd if max_dd > 0 else float("inf")

            # 夏普比率（简化版，假设无风险利率 2%，年化）
            daily_rets = []
            for i in range(1, len(equity_log)):
                prev = equity_log[i-1][1]
                curr = equity_log[i][1]
                if prev > 0:
                    daily_rets.append((curr - prev) / prev)
            if len(daily_rets) > 1:
                import statistics
                avg_d  = statistics.mean(daily_rets)
                std_d  = statistics.stdev(daily_rets)
                rf_d   = 0.02 / 252     # 2% 年化无风险利率
                sharpe = (avg_d - rf_d) / std_d * math.sqrt(252) if std_d > 0 else 0.0

            ret_icon   = "🟢" if total_ret >= 0 else "🔴"
            cal_icon   = "🟢" if calmar >= 1.0 else ("🟡" if calmar >= 0.5 else "🔴")
            sharpe_icon = "🟢" if sharpe >= 1.0 else ("🟡" if sharpe >= 0.5 else "🔴")

            print(f"\n【账户概览】")
            print(f"  初始资金:         {self.init_capital:>12,.0f} 元")
            print(f"  最终净值:         {final_eq:>12,.0f} 元")
            print(f"  {ret_icon} 总收益率:      {total_ret:>+11.2f} %")
            print(f"  {ret_icon} 年化收益率:    {annual_ret:>+11.2f} %  "
                  f"（回测 {trading_days} 交易日）")
            print(f"  最大回撤:         {max_dd:>11.2f} %  "
                  f"（最长持续 {max_dd_days} 天）")
            print(f"  {cal_icon} 卡玛比率:      {calmar:>11.2f}    "
                  f"（年化收益/最大回撤，>1 为优）")
            print(f"  {sharpe_icon} 夏普比率:      {sharpe:>11.2f}    "
                  f"（>1 为优，无风险利率 2%）")
            if market_blocked_days > 0 and trading_days > 0:
                block_pct = market_blocked_days / trading_days * 100
                print(f"  🛡 大盘过滤:    屏蔽建仓 {market_blocked_days:>4} 天 / "
                      f"共 {trading_days} 天 ({block_pct:.0f}%，HS300<MA60期间)")

        # ── 交易统计 ──────────────────────────────────
        if not trades:
            print("\n  回测期间无已完成交易\n")
        else:
            wins   = [t for t in trades if t.pnl > 0]
            losses = [t for t in trades if t.pnl <= 0]
            total  = len(trades)

            avg_win  = sum(t.pnl_pct for t in wins)   / len(wins)   if wins   else 0.0
            avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0.0
            win_rate = len(wins) / total
            loss_rate = 1 - win_rate

            # 期望值 = 胜率×平均盈利% - 败率×平均亏损%
            expectancy = win_rate * avg_win + loss_rate * avg_loss   # avg_loss 已为负

            # 盈亏比 = |平均盈利| / |平均亏损|
            profit_factor = abs(avg_win) / abs(avg_loss) if avg_loss != 0 else float("inf")

            # 最大连续亏损
            max_consec_loss = cur_consec = 0
            for t in trades:
                if t.pnl <= 0:
                    cur_consec += 1
                    max_consec_loss = max(max_consec_loss, cur_consec)
                else:
                    cur_consec = 0

            # 出场信号分布
            exit_cnt: dict[str, int] = {}
            for t in trades:
                exit_cnt[t.exit_signal] = exit_cnt.get(t.exit_signal, 0) + 1

            pf_icon = "🟢" if profit_factor >= 1.5 else ("🟡" if profit_factor >= 1.0 else "🔴")
            exp_icon = "🟢" if expectancy > 0 else "🔴"

            print(f"\n【交易统计】")
            print(f"  总笔数:           {total}")
            print(f"  胜率:             {win_rate*100:.1f}%  ({len(wins)} 胜 / {len(losses)} 负)")
            print(f"  {pf_icon} 盈亏比:        {profit_factor:>8.2f}    "
                  f"（平均盈利%/平均亏损%，>1.5 为优）")
            print(f"  {exp_icon} 期望值:        {expectancy:>+7.2f} %  "
                  f"（每笔平均期望收益，>0 策略有效）")
            print(f"  最大连续亏损:     {max_consec_loss} 笔")
            print(f"  平均持仓:         {sum(t.hold_days for t in trades)/total:.1f} 天")
            print(f"  总盈亏:           {sum(t.pnl for t in trades):>+.0f} 元")
            if wins:
                print(f"  平均盈利:         {sum(t.pnl for t in wins)/len(wins):>+.0f} 元  "
                      f"最大: {max(t.pnl for t in wins):>+.0f} 元 ({max(t.pnl_pct for t in wins):+.1f}%)")
            if losses:
                print(f"  平均亏损:         {sum(t.pnl for t in losses)/len(losses):>+.0f} 元  "
                      f"最大: {min(t.pnl for t in losses):>+.0f} 元 ({min(t.pnl_pct for t in losses):+.1f}%)")

            print(f"\n  出场信号分布:")
            for sig, cnt in sorted(exit_cnt.items(), key=lambda x: -x[1]):
                bar = "█" * cnt
                print(f"    {sig:<14} {cnt:>3} 笔  {bar}")

            print(f"\n【逐笔交易记录】")
            print(f"  {'股票':<12} {'信号日':<11} {'买入日':<11} {'卖出日':<11} "
                  f"{'买价':>7} {'卖价':>7} {'股数':>6} "
                  f"{'盈亏(元)':>10} {'收益%':>7} {'持有天':>6}  出场信号")
            print(f"  {'-'*110}")
            for t in trades:
                flag = "🟢" if t.pnl > 0 else "🔴"
                print(f"  {flag} {t.name}({t.code})  "
                      f"{t.signal_date}  {t.buy_date}  {t.sell_date}  "
                      f"{t.buy_price:>7.2f}  {t.sell_price:>7.2f}  "
                      f"{t.shares:>6}  "
                      f"{t.pnl:>+10.0f}  "
                      f"{t.pnl_pct:>+6.1f}%  "
                      f"{t.hold_days:>5}天  "
                      f"{t.exit_signal}")

        # ── 基准对比 ──────────────────────────────────
        if equity_log and benchmark_ret != 0.0:
            total_ret   = (equity_log[-1][1] - self.init_capital) / self.init_capital * 100
            alpha       = total_ret - benchmark_ret
            alpha_icon  = "🟢" if alpha >= 0 else "🔴"
            bm_icon     = "🟢" if benchmark_ret >= 0 else "🔴"
            print(f"\n【基准对比（沪深300 ETF 同期）】")
            print(f"  {bm_icon} 沪深300同期:   {benchmark_ret:>+10.2f} %")
            print(f"  {'🟢' if total_ret>=0 else '🔴'} 策略总收益:   {total_ret:>+10.2f} %")
            print(f"  {alpha_icon} 超额收益Alpha: {alpha:>+10.2f} %  "
                  f"（{'跑赢' if alpha >= 0 else '跑输'}大盘）")

        # 回测结束时未平仓
        if open_pos:
            print(f"\n【回测结束时未平仓】")
            for code, pos in open_pos.items():
                cur  = last_prices.get(code, pos["buy_price"])
                pct  = (cur - pos["buy_price"]) / pos["buy_price"] * 100
                flag = "🟢" if pct >= 0 else "🔴"
                print(f"  {flag} {pos['name']}({code})  "
                      f"信号{pos['signal_date']}  {pos['signal']}  "
                      f"买入@{pos['buy_price']:.2f}  "
                      f"最新@{cur:.2f}  持有{pos['hold_days']}天  浮盈{pct:+.1f}%")

        print(f"\n{'='*65}\n")
