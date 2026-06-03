"""
Phase 2 · 完整组合级忠实回测（分钟级回放实盘进出场逻辑）
================================================
与日线回测(开盘/收盘撮合)不同，本回测在真实 5 分钟K上逐根运行实盘的
check_entry / check_position，并复刻追踪止损、6 仓限制、T+1、佣金+印花，
得到"与实盘进出场逻辑完全一致"的结果。

数据：~/.520quant/intraday.db（需先用 backfill_baostock 回补 5 分钟历史）。
宇宙：build_universe(sample, seed)，须与回补的样本一致。

简化与口径（务必知悉）：
  - 量比按 1.0 放行（5分钟量与日线量纲不一致，无法精确重建）→ 进场略宽松。
  - 相对强弱 market_chg 置 0 → 同上。
  - 候选优先级简化为"粘合优先 → 评分降序"（实盘是盈亏比优先，影响二阶）。
  - 日线 MA 取截至 T-1（与实盘候选/持仓所用的最近完整日线一致）。
  - 复权：5分钟与日线均不复权，口径一致。

用法:
  python -m backtest.intraday_portfolio --sample 150 --seed 42
        --start 2025-05-22 --end 2026-06-02 --max-pos 6 --slippage 0.0
"""
from __future__ import annotations

import sys
import bisect
import argparse
import statistics
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from data.fetcher import db
from data import intraday_store as ids
from strategy.signal_520 import strategy, Signal
import monitor.intraday as IT
from monitor.intraday import engine as IE, Action
from monitor.realtime import is_buy_window as _RBW, is_profit_exit_window as _RPEW
from monitor.engine import SIGNAL_SIZE, MonitorEngine
from backtest.replay import build_universe

COMMISSION = 0.0001   # 万1 佣金（买卖双向）
STAMP_TAX  = 0.001
TIERS = MonitorEngine._TRAIL_TIERS
BUY_SIGS = (Signal.BUY_GOLDEN_CROSS, Signal.BUY_PULLBACK, Signal.BUY_SQUEEZE)

# ── 回放上下文 + 打桩（让实盘函数读 point-in-time 数据 / 按 bar 时间判窗）──
_CTX = {"dt": None, "mindf": None}
IT.is_buy_window         = lambda now=None, signal_type="": _RBW(now=_CTX["dt"], signal_type=signal_type)
IT.is_profit_exit_window = lambda now=None: _RPEW(now=_CTX["dt"])
IT.get_minute_bars       = lambda code, freq="5m", count=20: (
    _CTX["mindf"].tail(count) if _CTX["mindf"] is not None else pd.DataFrame())


def _elapsed_min(dt_str: str) -> int:
    """从开盘到该 5 分钟K结束时刻的累计交易分钟（午休不计）"""
    hh, mm = int(dt_str[11:13]), int(dt_str[14:16])
    t = hh * 60 + mm
    open1, close1, open2 = 9 * 60 + 30, 11 * 60 + 30, 13 * 60
    if t <= close1:
        return max(0, t - open1)
    return 120 + max(0, t - open2)


def _sig_mult(sig: str) -> float:
    if "粘合" in sig or "发散" in sig:
        return SIGNAL_SIZE[Signal.BUY_SQUEEZE]
    if "回踩" in sig:
        return SIGNAL_SIZE[Signal.BUY_PULLBACK]
    return SIGNAL_SIZE[Signal.BUY_GOLDEN_CROSS]


def _update_stop(pos: dict, price: float, ma5: float):
    """复刻 engine._update_trailing_stops：止损线只升不降"""
    gain = (price - pos["cost"]) / pos["cost"] * 100
    cand = pos["stop"]
    for min_gain, _label, mult in TIERS:
        if gain >= min_gain:
            base = pos["cost"] * mult
            cand = max(base, round(ma5 * 0.97, 2)) if (min_gain >= 10 and ma5 > 0) else base
            break
    if cand > pos["stop"]:
        pos["stop"] = round(cand, 2)


def _load(code: str):
    """返回 (daily_df_with_date, m5_by_date, date_list)"""
    daily = db.get(code, freq="day", bars=320)
    if daily is None or daily.empty:
        return None, None, None
    daily = daily.copy()
    daily["d"] = daily["datetime"].astype(str).str[:10]
    bars = ids.get_bars(code, "5m")            # [(dt,o,h,l,c,v), ...]
    if not bars:
        return None, None, None
    m5 = {}
    for dt, o, h, l, c, v in bars:
        d = dt[:10]
        m5.setdefault(d, []).append((dt, o, h, l, c, v))
    return daily, m5, daily["d"].tolist()


def _precompute_candidates(daily: pd.DataFrame) -> dict:
    """走一遍日线：返回 {候选日: (信号类型, asof_end)}，asof_end=daily.iloc[:asof_end] 即截至T-1"""
    out = {}
    n = len(daily)
    dlist = daily["d"].tolist()
    for j in range(24, n - 1):
        res = strategy.analyze(daily.iloc[: j + 1])
        if res.signal in BUY_SIGS:
            out[dlist[j + 1]] = (res.signal.value, j + 1, res.score or 0)  # (信号, asof, 评分)
    return out


def load_ctx(codes):
    """一次性加载：日线 + 5分钟 + 候选 + 各日全天量。返回 ctx 供多次 simulate 复用。"""
    daily_map, m5_map, cand_map, day_total, sdates = {}, {}, {}, {}, {}
    for c in codes:
        d, m5, _ = _load(c)
        if d is None:
            continue
        daily_map[c] = d
        m5_map[c] = m5
        cand_map[c] = _precompute_candidates(d)
        day_total[c] = {dt: sum(float(b[5]) for b in bars) for dt, bars in m5.items()}
        sdates[c] = sorted(m5.keys())
    mk = db.get_market(bars=320).copy()
    mk["d"] = mk["datetime"].astype(str).str[:10]
    return {"daily": daily_map, "m5": m5_map, "cand": cand_map,
            "day_total": day_total, "sdates": sdates, "mk": mk}


def simulate(ctx, names, start, end, max_pos=6, capital=200_000, slippage=0.0,
             top_n=8):
    """在 ctx 上跑一次忠实回测（使用 intraday 模块当前的止损/止盈参数）。
    top_n: 每日"精选"上限——只在评分最高的 top_n 只信号股里建仓（0=不设上限）。
    返回 (trades, equity_curve, positions)。"""
    daily_map = ctx["daily"]; m5_map = ctx["m5"]; cand_map = ctx["cand"]
    day_total = ctx["day_total"]; sdates = ctx["sdates"]; mk = ctx["mk"]

    def _risk_dist(code, sig, asof):
        """到止损距离%（盈亏比代理）：回踩/粘合用MA20、金叉用MA5×0.97；越小越优先"""
        last = daily_map[code].iloc[asof - 1]
        px = float(last["close"]); ma20 = float(last["ma20"]); ma5 = float(last["ma5"])
        stop_ref = ma20 if ("粘合" in sig or "发散" in sig or "回踩" in sig) else ma5 * 0.97
        if px <= 0 or stop_ref <= 0 or stop_ref >= px:
            return 9999.0
        return (px - stop_ref) / px * 100

    def vol_ratio(code, T, i, day):
        elapsed = _elapsed_min(day[i][0])
        if elapsed <= 0:
            return 0.0
        cum = sum(float(day[j][5]) for j in range(i + 1))
        sd = sdates[code]
        idx = bisect.bisect_left(sd, T)
        prior = sd[max(0, idx - 5):idx]
        if not prior:
            return 1.0
        avg = sum(day_total[code][p] for p in prior) / len(prior)
        return (cum / elapsed) / (avg / 240.0) if avg > 0 else 1.0

    def market_up(date_t):
        sub = mk[mk["d"] < date_t]
        if sub.empty:
            return True
        last = sub.iloc[-1]
        try:
            return float(last["ma20"]) > float(last["ma60"])
        except Exception:
            return True

    def daily_asof(code, date_t):
        d = daily_map[code]
        sub = d[d["d"] < date_t]
        return sub if len(sub) >= 25 else None

    all_dates = set()
    for c in m5_map:
        all_dates |= set(m5_map[c].keys())
    sim_dates = sorted(d for d in all_dates if start <= d <= end)
    date_idx = {d: i for i, d in enumerate(sim_dates)}   # 交易日序号，用于算持有天数

    cash = float(capital)
    grid = capital / max_pos
    positions: dict[str, dict] = {}
    trades = []
    equity_curve = []

    for T in sim_dates:
        mkt_ok = market_up(T)
        # 当日候选（粘合优先，再评分降序——评分用 analyze 复算一次）
        todays = []
        for c in daily_map:
            if T in cand_map[c]:
                sig, asof, score = cand_map[c][T]
                todays.append((c, sig, asof, score))
        # A: 精选 Top-N —— 按评分降序只保留前 top_n 只（复刻实盘"只买精选"）
        if top_n and top_n > 0 and len(todays) > top_n:
            todays.sort(key=lambda it: -it[3])
            todays = todays[:top_n]
        # B: 买入优先级 —— 粘合优先 → 盈亏比(到止损距离)升序（与实盘一致）
        todays.sort(key=lambda it: (0 if ("粘合" in it[1] or "发散" in it[1]) else 1,
                                    _risk_dist(it[0], it[1], it[2])))

        for i in range(48):
            # ── 出场（先于进场，释放仓位）──
            for code in list(positions):
                pos = positions[code]
                if pos["entry_date"] == T:
                    continue   # T+1
                day = m5_map[code].get(T)
                if not day or i >= len(day):
                    continue
                sub = daily_asof(code, T)
                if sub is None:
                    continue
                bar = day[i]
                price = float(bar[4])
                ts = datetime.strptime(bar[0], "%Y-%m-%d %H:%M:%S")
                last = sub.iloc[-1]
                last_close = float(last["close"]); ma5 = float(last["ma5"])
                chg = (price - last_close) / last_close * 100 if last_close else 0.0
                pnl = (price - pos["cost"]) / pos["cost"] * 100
                pos["peak"] = max(pos["peak"], pnl)
                if ("粘合" in pos["sig"] or "发散" in pos["sig"]) and chg >= 9.0 and not pos["flu"]:
                    pos["flu"] = T
                _update_stop(pos, price, ma5)
                runhigh = max(float(day[j][2]) for j in range(i + 1))
                runlow  = min(float(day[j][3]) for j in range(i + 1))
                _CTX["dt"] = ts
                _CTX["mindf"] = pd.DataFrame(
                    {"close": [float(day[j][4]) for j in range(i + 1)]})
                quote = {"price": price, "last_close": last_close, "open": float(day[0][1]),
                         "high": runhigh, "low": runlow, "change_pct": chg, "vol_ratio": 1.0}
                hd = date_idx.get(T, 0) - date_idx.get(pos["entry_date"], 0)
                sig = IE.check_position(code, sub, quote, cost=pos["cost"],
                                        stop_price=pos["stop"], peak_pnl=pos["peak"],
                                        entry_signal=pos["sig"], first_limit_up_date=pos["flu"],
                                        hold_days=hd)
                if sig.action in (Action.SELL_STOP, Action.SELL_PROFIT):
                    exec_p = round(price * (1 - slippage), 3)
                    gross = exec_p * pos["shares"]
                    fee = round(gross * (COMMISSION + STAMP_TAX), 2)
                    net = gross - fee
                    cost_amt = pos["cost"] * pos["shares"]
                    tpnl = round(net - cost_amt, 2)
                    cash += net
                    trades.append({"code": code, "name": names.get(code, code),
                                   "buy_date": pos["entry_date"], "sell_date": T,
                                   "buy": pos["cost"], "sell": exec_p, "pnl": tpnl,
                                   "pnl_pct": round(tpnl / cost_amt * 100, 2),
                                   "sig": pos["sig"], "exit": sig.action.name})
                    del positions[code]

            # ── 进场 ──
            if mkt_ok:
                for code, stype, asof, _score in todays:
                    if code in positions or len(positions) >= max_pos:
                        continue
                    day = m5_map[code].get(T)
                    if not day or i >= len(day):
                        continue
                    sub = daily_map[code].iloc[:asof]
                    bar = day[i]
                    price = float(bar[4])
                    ts = datetime.strptime(bar[0], "%Y-%m-%d %H:%M:%S")
                    last_close = float(sub.iloc[-1]["close"])
                    chg = (price - last_close) / last_close * 100 if last_close else 0.0
                    runhigh = max(float(day[j][2]) for j in range(i + 1))
                    runlow  = min(float(day[j][3]) for j in range(i + 1))
                    _CTX["dt"] = ts
                    _CTX["mindf"] = pd.DataFrame(
                        {"close": [float(day[j][4]) for j in range(i + 1)]})
                    vr = vol_ratio(code, T, i, day)
                    quote = {"price": price, "last_close": last_close, "open": float(day[0][1]),
                             "high": runhigh, "low": runlow, "change_pct": chg, "vol_ratio": vr}
                    sig = IE.check_entry(code, sub, quote, signal_type=stype, market_chg=0.0)
                    if sig.action == Action.BUY:
                        exec_p = round(price * (1 + slippage), 3)
                        slot = grid * _sig_mult(stype)
                        shares = int(slot / exec_p / 100) * 100
                        if shares < 100:
                            continue
                        amt = exec_p * shares
                        fee = round(amt * COMMISSION, 2)
                        if cash < amt + fee:
                            continue
                        cash -= amt + fee
                        positions[code] = {"cost": exec_p, "shares": shares,
                                           "stop": round(exec_p * 0.95, 2), "peak": 0.0,
                                           "sig": stype, "entry_date": T, "flu": ""}

        # 当日收盘市值（按各股当日最后一根5分钟收盘）
        pv = 0.0
        for code, pos in positions.items():
            day = m5_map[code].get(T)
            px = float(day[-1][4]) if day else pos["cost"]
            pv += px * pos["shares"]
        equity_curve.append((T, round(cash + pv, 2)))

    return trades, equity_curve, positions


def metrics(trades, equity_curve, positions, capital, mk) -> dict:
    """汇总指标字典（供 _report 与 sweep 共用）"""
    if not equity_curve:
        return {}
    final_eq = equity_curve[-1][1]
    ret = (final_eq - capital) / capital * 100
    peak = capital; mdd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak * 100)
    wins = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    avg_w = statistics.mean([t["pnl_pct"] for t in wins]) if wins else 0.0
    avg_l = statistics.mean([t["pnl_pct"] for t in losers]) if losers else 0.0
    seg = mk[(mk["d"] >= equity_curve[0][0]) & (mk["d"] <= equity_curve[-1][0])]
    bench = ((float(seg.iloc[-1]["close"]) - float(seg.iloc[0]["close"]))
             / float(seg.iloc[0]["close"]) * 100) if len(seg) else 0.0
    from collections import Counter
    return {"ret": round(ret, 2), "mdd": round(mdd, 2),
            "calmar": round(ret / mdd, 2) if mdd else 0.0,
            "trades": len(trades),
            "win": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "pnl_ratio": round(abs(avg_w / avg_l), 2) if avg_l else 0.0,
            "alpha": round(ret - bench, 2), "bench": round(bench, 2),
            "open_pos": len(positions),
            "exits": dict(Counter(t["exit"] for t in trades))}


def _report(m, equity_curve):
    print("\n" + "=" * 66)
    print("  Phase 2 忠实回测结果（分钟级回放实盘进出场）")
    print("=" * 66)
    if not m:
        print("  无交易日数据"); return
    print(f"  区间: {equity_curve[0][0]} ~ {equity_curve[-1][0]} ({len(equity_curve)}日)")
    print(f"  总收益: {m['ret']:+.2f}%   最大回撤: {m['mdd']:.2f}%   卡玛: {m['calmar']}")
    print(f"  交易: {m['trades']}笔  胜率: {m['win']}%  盈亏比: {m['pnl_ratio']}  未平仓: {m['open_pos']}只")
    print(f"  出场分布: {m['exits']}")
    print(f"  沪深300同期: {m['bench']:+.2f}%   超额Alpha: {m['alpha']:+.2f}%")
    print("\n⚠️ 量比已按5分钟量重建 / 相对强弱跳过 / 优先级简化——进出场时点为真实分钟级。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", default="2025-05-22")
    ap.add_argument("--end", default="2026-06-02")
    ap.add_argument("--max-pos", type=int, default=6)
    ap.add_argument("--capital", type=float, default=200_000)
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--top-n", type=int, default=8, help="每日精选上限(0=不限)")
    a = ap.parse_args()

    universe = build_universe(a.sample, a.seed)
    names = {s["code"]: s["name"] for s in universe}
    print(f"忠实回测：{len(names)} 只 | {a.start}~{a.end} | {a.max_pos}仓 | "
          f"精选Top{a.top_n} | 滑点{a.slippage*100:.1f}%")
    ctx = load_ctx(list(names))
    print(f"  有效数据: {len(ctx['daily'])} 只")
    trades, ec, pos = simulate(ctx, names, a.start, a.end, a.max_pos, a.capital,
                               a.slippage, top_n=a.top_n)
    _report(metrics(trades, ec, pos, a.capital, ctx["mk"]), ec)


if __name__ == "__main__":
    main()
