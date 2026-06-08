"""
尾盘建仓 vs 次日盘中建仓 — 全样本多窗口验证
================================================
现行：信号日收盘形成回踩 → 次日10:00-14:30盘中确认后买（会被次日跳空甩开）。
尾盘：信号日当天最后一根5分钟K(≈尾盘14:55)直接买（避开次日跳空）。
本回测尾盘用"当天真实收盘价"判信号=14:45完美预测收盘的【乐观上界】。
固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%。
用法: python3 -B -m backtest.validate_tail
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


def _row(ctx, names, s, e, tail):
    t, ec, pos = simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=SLIP,
                          top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk",
                          tail_entry=tail)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes); names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只\n")

    win = 0
    for label, s, e in WINDOWS:
        m0 = _row(ctx, names, s, e, False)   # 现行:次日盘中
        m1 = _row(ctx, names, s, e, True)    # 尾盘:信号日收盘
        better = m1["calmar"] >= m0["calmar"]
        win += 1 if better else 0
        print(f"── {label} ({s}~{e})")
        print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, m in (("现行(次日盘中)", m0), ("尾盘(信号日收盘)", m1)):
            print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print(f"   → 尾盘 {'≥' if better else '<'} 现行 (卡玛{m1['calmar']} vs {m0['calmar']})\n")

    print("=" * 60)
    print(f"尾盘(乐观上界) ≥ 现行：{win}/{len(WINDOWS)} 窗口")
    print("注：尾盘为'14:45完美预测收盘'乐观上界；若此处不占优则直接否决。")


if __name__ == "__main__":
    main()
