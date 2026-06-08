"""
追踪止损阶梯对照 — 全样本 + 多时间窗口验证（不抽样，选股忠实）
================================================
修正方法论：不随机抽半样本(会改变 top_n 竞争池)，而是全 1055 只不动，
按时间切多个窗口做稳健性验证。
现行 vs 提案(密集锁利)。固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%·封顶100。
用法: python -m backtest.validate_trailtiers_full
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
SLIP = 0.001

# 全样本、按时间切窗口（每个窗口都用全部股票，选股忠实）
WINDOWS = [
    ("全期",   "2025-05-22", "2026-06-02"),
    ("Q1夏",   "2025-05-22", "2025-08-31"),
    ("Q2秋",   "2025-09-01", "2025-11-30"),
    ("Q3冬",   "2025-12-01", "2026-02-28"),
    ("Q4春",   "2026-03-01", "2026-06-02"),
]

TIERS_CUR  = [(30.0, "+20%", 1.20), (20.0, "+13%", 1.13), (10.0, "+5%", 1.05), (5.0, "保本", 1.002)]
TIERS_USER = [(20.0, "+15%", 1.15), (15.0, "+11%", 1.11), (10.0, "+7%", 1.07), (5.0, "+5%", 1.05)]


def _row(ctx, names, start, end, tiers):
    t, ec, pos = simulate(ctx, names, start, end, max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk", tiers=tiers, ma5_min=10.0)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本，不抽样）…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只\n")

    win = 0; total = 0
    for label, start, end in WINDOWS:
        m0 = _row(ctx, names, start, end, TIERS_CUR)
        m1 = _row(ctx, names, start, end, TIERS_USER)
        if not m0 or not m1:
            print(f"── {label}: 无足够数据，跳过\n"); continue
        total += 1
        better = m1["calmar"] >= m0["calmar"]
        win += 1 if better else 0
        print(f"── {label} ({start}~{end})")
        print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, m in (("现行阶梯", m0), ("提案密集锁利", m1)):
            print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print(f"   → 提案 {'≥' if better else '<'} 现行 (卡玛{m1['calmar']} vs {m0['calmar']})\n")

    print("=" * 60)
    print(f"提案 ≥ 现行：{win}/{total} 个时间窗口（全样本，选股忠实）")


if __name__ == "__main__":
    main()
