"""
追踪止损分档对照：现行(10%→锁+5%) vs 提案(8%→锁+5%)
================================================
其余实盘参数一致：全样本 · max_pos=4 · 分批+12% · 评分优先 · MA20止损。
MA5 跟随阈值随之挪动（现行≥10% / 提案≥8%），口径忠实。

用法: python -m backtest.tier_test
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
START, END = "2025-05-22", "2026-06-02"

# 现行分档（与 engine._TRAIL_TIERS 一致）
TIERS_CUR = [
    (30.0, "锁+20%", 1.20),
    (20.0, "锁+13%", 1.13),
    (10.0, "锁+5%",  1.05),
    ( 5.0, "保本",   1.002),
]
# 提案：10%档 → 8%档（浮盈到8%即锁+5%）
TIERS_PROP = [
    (30.0, "锁+20%", 1.20),
    (20.0, "锁+13%", 1.13),
    ( 8.0, "锁+5%",  1.05),
    ( 5.0, "保本",   1.002),
]


def run(ctx, names, label, tiers, ma5_min, slippage):
    t, ec, pos = simulate(ctx, names, START, END, max_pos=4, capital=CAP,
                          slippage=slippage, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="score", tiers=tiers, ma5_min=ma5_min)
    m = metrics(t, ec, pos, CAP, ctx["mk"]); m["_label"] = label
    return m


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只 | {START}~{END} | 全样本·4仓·分批12%·评分优先\n")

    rows = [
        run(ctx, names, "现行 10%→锁5% ·滑点0",   TIERS_CUR,  10.0, 0.000),
        run(ctx, names, "提案 8%→锁5%  ·滑点0",   TIERS_PROP,  8.0, 0.000),
        run(ctx, names, "现行 10%→锁5% ·滑点0.1%", TIERS_CUR,  10.0, 0.001),
        run(ctx, names, "提案 8%→锁5%  ·滑点0.1%", TIERS_PROP,  8.0, 0.001),
    ]

    print("=" * 88)
    print("  追踪止损分档对照：10%→锁5%  vs  8%→锁5%")
    print("=" * 88)
    print(f"  {'方案':<22}{'收益%':>8}{'回撤%':>8}{'卡玛':>7}{'Alpha%':>9}"
          f"{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
    for m in rows:
        print(f"  {m['_label']:<22}{m['ret']:>8.1f}{m['mdd']:>8.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>9.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
    print(f"\n  沪深300同期: {rows[0].get('bench'):+.1f}%")
    print("\n⚠️ 单区间(偏牛)单宇宙；如倾向上线，建议再跨样本/跨期复核防过拟合。")


if __name__ == "__main__":
    main()
