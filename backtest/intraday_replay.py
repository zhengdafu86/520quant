"""
Phase 2 · 分钟级忠实回放（进场价差研究）
================================================
回答核心问题：实盘"盘中确认后才买"，比回测假设的"次日开盘价成交"到底贵多少、
有多少日线信号根本不会被盘中确认。

做法：对每个历史日线买点，取当日真实 5 分钟K，逐根跑真实 check_entry
（打桩 get_minute_bars / is_buy_window 喂 point-in-time 数据），
找出"第一次触发 BUY 的那根K的价格与时间" = 实盘真实进场，再对比当日开盘价。

约束（务必知悉）：
  - 腾讯只给 ~7 个交易日 5 分钟历史，样本小，仅作机制演示与价差量化。
  - 量比按 1.0 放行（5分钟量与日线量纲不一致，无法精确重建）→ 进场略偏宽松。
  - 相对强弱(market_chg)置 0 跳过 → 同上。
  - 长周期需靠每日 15:40 自动落库积累后，改用本地 intraday.db 回放。

用法:
  python -m backtest.intraday_replay --codes 600036,000001,002803
  python -m backtest.intraday_replay --codes <...> --signal 回踩
"""
from __future__ import annotations

import sys
import argparse
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from data.fetcher import db, fetch_minute
from strategy.signal_520 import strategy, Signal
import monitor.intraday as IT
from monitor.intraday import engine as intraday_engine, Action
from monitor.realtime import is_buy_window as _real_is_buy_window


# ── 回放期间打桩用的可变状态 ──────────────────────────
_CTX = {"bar_dt": None, "min_df": None}


def _patched_is_buy_window(now=None, signal_type=""):
    return _real_is_buy_window(now=_CTX["bar_dt"], signal_type=signal_type)


def _patched_get_minute_bars(code, freq="5m", count=20):
    df = _CTX["min_df"]
    return df.tail(count) if df is not None else pd.DataFrame()


def _daily_signal_asof(daily: pd.DataFrame, before_date: str):
    """截至 before_date(不含当日) 的日线信号；返回 (signal_type, sub_df) 或 (None, None)"""
    sub = daily[daily["datetime"].astype(str).str[:10] < before_date]
    if len(sub) < 25:
        return None, None
    res = strategy.analyze(sub)
    if res.signal in (Signal.BUY_GOLDEN_CROSS, Signal.BUY_PULLBACK, Signal.BUY_SQUEEZE):
        return res.signal.value, sub
    return None, None


def replay_stock(code: str, want_signal: str = "") -> list[dict]:
    """对单只股票，在可用 5 分钟历史的每个交易日做进场回放"""
    daily = db.get(code, freq="day", bars=65)
    if daily is None or daily.empty:
        return []
    m5 = fetch_minute(code, freq="5m", count=320)
    if m5.empty:
        return []
    m5 = m5.copy()
    m5["date"] = m5["datetime"].str[:10]
    obs = []

    for date_t, day_bars in m5.groupby("date"):
        sig_type, sub_daily = _daily_signal_asof(daily, date_t)
        if not sig_type:
            continue
        if want_signal and want_signal not in sig_type:
            continue

        day_bars = day_bars.sort_values("datetime").reset_index(drop=True)
        last_close = float(sub_daily.iloc[-1]["close"])
        day_open   = float(day_bars.iloc[0]["open"])

        entry_price = None
        entry_time  = None
        cum_high = -1e9
        cum_low  = 1e9
        for i in range(len(day_bars)):
            row = day_bars.iloc[i]
            price = float(row["close"])
            cum_high = max(cum_high, float(row["high"]))
            cum_low  = min(cum_low,  float(row["low"]))
            chg = (price - last_close) / last_close * 100 if last_close else 0.0
            bar_dt = pd.to_datetime(row["datetime"]).to_pydatetime()

            quote = {
                "price": price, "last_close": last_close, "open": day_open,
                "high": cum_high, "low": cum_low, "change_pct": chg,
                "vol_ratio": 1.0,   # 放行（无法精确重建），见文件头说明
            }
            _CTX["bar_dt"] = bar_dt
            _CTX["min_df"] = day_bars.iloc[: i + 1][["datetime", "close"]]

            sig = intraday_engine.check_entry(
                code, sub_daily, quote, signal_type=sig_type, market_chg=0.0)
            if sig.action == Action.BUY:
                entry_price = price
                entry_time  = str(row["datetime"])[11:16]
                break

        obs.append({
            "code": code, "date": date_t, "signal": sig_type,
            "open": round(day_open, 3),
            "confirmed": entry_price is not None,
            "entry": round(entry_price, 3) if entry_price else None,
            "entry_time": entry_time,
            "gap_pct": round((entry_price / day_open - 1) * 100, 2) if entry_price else None,
        })
    return obs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="逗号分隔股票代码")
    ap.add_argument("--signal", default="", help="只看某类信号（回踩/金叉/粘合），空=全部")
    a = ap.parse_args()

    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    print(f"分钟级进场回放：{len(codes)} 只股票，信号过滤={a.signal or '全部'}")

    # 打桩
    _orig_bw, _orig_mb = IT.is_buy_window, IT.get_minute_bars
    IT.is_buy_window = _patched_is_buy_window
    IT.get_minute_bars = _patched_get_minute_bars
    try:
        all_obs = []
        for c in codes:
            try:
                all_obs += replay_stock(c, a.signal)
            except Exception as e:
                print(f"  {c} 回放失败: {e}")
    finally:
        IT.is_buy_window, IT.get_minute_bars = _orig_bw, _orig_mb

    if not all_obs:
        print("无可回放的信号样本（可能是近 7 天内这些票无日线买点）")
        return

    confirmed = [o for o in all_obs if o["confirmed"]]
    gaps = [o["gap_pct"] for o in confirmed]

    print("\n" + "=" * 66)
    print("  分钟级进场回放结果")
    print("=" * 66)
    print(f"  日线信号样本:       {len(all_obs)} 个")
    print(f"  盘中确认成交:       {len(confirmed)} 个 "
          f"({len(confirmed)/len(all_obs)*100:.0f}%)")
    print(f"  全天未确认(被过滤): {len(all_obs)-len(confirmed)} 个 "
          f"({(len(all_obs)-len(confirmed))/len(all_obs)*100:.0f}%)")
    if gaps:
        print(f"\n  实际进场价 vs 当日开盘价（正=比开盘贵，即回测高估的部分）:")
        print(f"    平均: {statistics.mean(gaps):+.2f}%   中位: {statistics.median(gaps):+.2f}%")
        print(f"    最贵: {max(gaps):+.2f}%   最便宜: {min(gaps):+.2f}%")
        print(f"    >开盘价的比例: {sum(1 for g in gaps if g>0)/len(gaps)*100:.0f}%")

    print("\n  明细:")
    print(f"  {'代码':<8}{'日期':<12}{'信号':<10}{'开盘':>8}{'进场':>8}{'时间':>7}{'价差%':>8}")
    for o in sorted(all_obs, key=lambda x: (x["date"], x["code"])):
        e  = f"{o['entry']:.2f}" if o["entry"] else "未确认"
        t  = o["entry_time"] or "—"
        g  = f"{o['gap_pct']:+.2f}" if o["gap_pct"] is not None else "—"
        print(f"  {o['code']:<8}{o['date']:<12}{o['signal'][:8]:<10}"
              f"{o['open']:>8.2f}{e:>8}{t:>7}{g:>8}")

    print("\n⚠️ 样本仅 ~7 个交易日、量比放行、相对强弱跳过——机制演示与价差量化，"
          "非统计结论。长周期待 intraday.db 积累后回放。")


if __name__ == "__main__":
    main()
