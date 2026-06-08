"""
放松「+5%→保本」档 跨时间窗口验证（解决"涨5%后回踩到成本被震出"）
================================================
现行: ≥5%即把止损挪到保本 → 涨5%的票一回踩到成本就止损，容易被洗。
对照三个放松方案。纯卖出改动(tiers) → 共享 ctx。全样本不抽样。
固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%·封顶100。
用法: python3 -B -m backtest.validate_loosenstop
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
SLIP = 0.001
WINDOWS = [
    ("全期",  "2025-05-22", "2026-06-02"),
    ("Q1夏",  "2025-05-22", "2025-08-31"),
    ("Q2秋",  "2025-09-01", "2025-11-30"),
    ("Q3冬",  "2025-12-01", "2026-02-28"),
    ("Q4春",  "2026-03-01", "2026-06-02"),
]

VARIANTS = {
    "现行(5%保本)":  [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05), (5.0, "", 1.002)],
    "A去保本档":     [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05)],
    "B保本推到8%":   [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05), (8.0, "", 1.002)],
    "C5%→止损-2%":   [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05), (5.0, "", 0.98)],
}


def _row(ctx, names, start, end, tiers):
    t, ec, pos = simulate(ctx, names, start, end, max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk", tiers=tiers, ma5_min=10.0)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只\n")

    for label, s, e in WINDOWS:
        print(f"── {label} ({s}~{e})")
        print(f"   {'方案':<14}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, tiers in VARIANTS.items():
            m = _row(ctx, names, s, e, tiers)
            print(f"   {nm:<14}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print()
    print("=" * 60)
    print("看放松方案(A/B/C)是否在多数窗口收益/卡玛≥现行（减少被震出 vs 失败时多亏的权衡）。")


if __name__ == "__main__":
    main()
