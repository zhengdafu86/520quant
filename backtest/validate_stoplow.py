"""
盘中最低价止损口径 + 放松保本档 验证
================================================
① 量化"震出代价"：收盘口径 vs 盘中低价口径（都用现行5%保本）。
② 盘中低价口径下，比 现行保本 vs 放松方案(A去保本/B推到8%/C→-2%)，
   看放松能否减少被震出、提升收益。
固定:全样本·4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%。
用法: python3 -B -m backtest.validate_stoplow
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
T_CUR = [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05), (5.0, "", 1.002)]
T_A   = [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05)]
T_B   = [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05), (8.0, "", 1.002)]
T_C   = [(30.0, "", 1.20), (20.0, "", 1.13), (10.0, "", 1.05), (5.0, "", 0.98)]


def _row(ctx, names, s, e, tiers, low):
    t, ec, pos = simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=SLIP,
                          top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk",
                          tiers=tiers, ma5_min=10.0, stop_on_low=low)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes); names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只\n")

    print("=" * 70)
    print("① 震出代价：收盘口径 vs 盘中低价口径（均 5%保本，全期）")
    print("=" * 70)
    m_close = _row(ctx, names, "2025-05-22", "2026-06-02", T_CUR, False)
    m_low   = _row(ctx, names, "2025-05-22", "2026-06-02", T_CUR, True)
    print(f"  {'口径':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'交易':>6}{'胜率%':>7}")
    print(f"  {'收盘口径(现回测)':<16}{m_close['ret']:>8.1f}{m_close['mdd']:>7.1f}{m_close['calmar']:>7.2f}{m_close['trades']:>6}{m_close['win']:>7.1f}")
    print(f"  {'盘中低价口径':<16}{m_low['ret']:>8.1f}{m_low['mdd']:>7.1f}{m_low['calmar']:>7.2f}{m_low['trades']:>6}{m_low['win']:>7.1f}")
    print(f"  → 盘中低价口径更接近实盘；两者差距=被震出的代价\n")

    print("=" * 70)
    print("② 盘中低价口径下：现行保本 vs 放松方案（各时间窗口）")
    print("=" * 70)
    variants = [("现行5%保本", T_CUR), ("A去保本档", T_A), ("B保本推到8%", T_B), ("C 5%→-2%", T_C)]
    for label, s, e in WINDOWS:
        print(f"── {label}")
        print(f"   {'方案':<14}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, tiers in variants:
            m = _row(ctx, names, s, e, tiers, True)
            print(f"   {nm:<14}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print()


if __name__ == "__main__":
    main()
