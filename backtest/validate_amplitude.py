"""
入场「当日振幅上限」对照 — 全样本 + 多时间窗口验证（不抽样，选股忠实）
================================================
现行=5%。测 5 / 6 / 7 / 关闭(99)，看放宽振幅门槛对收益/回撤的影响。
入场过滤改动（不动候选/评分）→ 共享 ctx。
固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%·封顶100。
用法: python -m backtest.validate_amplitude
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import monitor.intraday as IT
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
CAPS = [5.0, 6.0, 7.0, 99.0]   # 99=实际关闭振幅过滤


def _row(ctx, names, start, end, amp):
    IT.AMPLITUDE_CAP = amp
    t, ec, pos = simulate(ctx, names, start, end, max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk")
    IT.AMPLITUDE_CAP = 5.0
    return metrics(t, ec, pos, CAP, ctx["mk"])


def _label(a):
    return "关闭" if a >= 99 else f"{a:.0f}%"


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本，不抽样）…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只 | 振幅门槛对照 {[_label(a) for a in CAPS]}\n")

    agg = {a: [] for a in CAPS}
    for label, start, end in WINDOWS:
        print(f"── {label} ({start}~{end})")
        print(f"   {'振幅门槛':<8}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for a in CAPS:
            m = _row(ctx, names, start, end, a)
            agg[a].append(m.get("calmar", 0))
            print(f"   {_label(a):<8}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print()

    print("=" * 60)
    print("各门槛 卡玛均值（全部窗口）：")
    for a in CAPS:
        v = agg[a]
        print(f"  振幅{_label(a):<6} 卡玛均值 {sum(v)/len(v):.2f}")
    print("现行=5%。均值最高者更优；但要看是否各窗口稳定，而非个别窗口拉高。")


if __name__ == "__main__":
    main()
