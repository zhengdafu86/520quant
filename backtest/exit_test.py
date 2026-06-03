"""
出场策略对照：ATR 自适应止损 / 分批止盈 vs 现行
================================================
在忠实回测引擎上对比几种出场方案（加载一次、跑多组）。
用库内全部已回补股票作稳定宇宙。

用法:
  python -m backtest.exit_test
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
START, END = "2025-05-22", "2026-06-02"

# (标签, atr_mult, scale_pct)
VARIANTS = [
    ("现行(MA20止损)", 0.0, 0.0),
    ("ATR止损×2",      2.0, 0.0),
    ("ATR止损×3",      3.0, 0.0),
    ("分批止盈+8%",     0.0, 8.0),
    ("分批+12%",        0.0, 12.0),
    ("ATR×2 + 分批+8%", 2.0, 8.0),
]


def main():
    codes = ids.all_codes("5m")
    names = {c: c for c in codes}
    print(f"出场对照：库内 {len(codes)} 只 | {START}~{END}")
    ctx = load_ctx(codes)
    print(f"  有效数据: {len(ctx['daily'])} 只\n")

    rows = []
    for label, atr, scale in VARIANTS:
        trades, ec, pos = simulate(ctx, names, START, END, max_pos=6, capital=CAP,
                                   top_n=8, atr_mult=atr, scale_pct=scale)
        m = metrics(trades, ec, pos, CAP, ctx["mk"])
        rows.append((label, m))
        print(f"  {label}: 收益{m['ret']:+.1f}% 回撤{m['mdd']:.1f}% 卡玛{m['calmar']} "
              f"Alpha{m['alpha']:+.1f}% 交易{m['trades']}笔 胜率{m['win']}%")

    print("\n" + "=" * 78)
    print("  出场策略对照（按卡玛=收益/回撤 降序）")
    print("=" * 78)
    print(f"  {'方案':<18}{'收益%':>8}{'回撤%':>8}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
    for label, m in sorted(rows, key=lambda r: r[1].get("calmar", -99), reverse=True):
        print(f"  {label:<18}{m['ret']:>8.1f}{m['mdd']:>8.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
    print("\n⚠️ 单区间单样本，须熊市段+另一样本复核才可下结论。")


if __name__ == "__main__":
    main()
