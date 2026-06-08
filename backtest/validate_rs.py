"""
RS 相对强度优先选股 — 全样本 + 多时间窗口验证
================================================
干净隔离：候选/评分/top_n 不变，只改买入排序——
  现行：粘合 → 盈亏比
  RS优先：粘合 → RS高优先 → 盈亏比   （粘合仍首位，RS作次级排序）
RS = 个股近20日收益 − 沪深300同期。纯卖出/排序改动 → 共享 ctx。
固定:4仓·分批12%·Top8·滑点0.1%·封顶100。
用法: python -m backtest.validate_rs
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


def _row(ctx, names, start, end, priority):
    t, ec, pos = simulate(ctx, names, start, end, max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority=priority)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只\n")

    win = 0
    for label, start, end in WINDOWS:
        m0 = _row(ctx, names, start, end, "squeeze_risk")   # 现行
        m1 = _row(ctx, names, start, end, "rs")              # 粘合→RS→盈亏比
        better = m1["calmar"] >= m0["calmar"]
        win += 1 if better else 0
        print(f"── {label} ({start}~{end})")
        print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, m in (("现行(粘合→盈亏比)", m0), ("RS优先(粘合→RS)", m1)):
            print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print(f"   → RS优先 {'≥' if better else '<'} 现行 (卡玛{m1['calmar']} vs {m0['calmar']})\n")

    print("=" * 60)
    print(f"RS优先 ≥ 现行：{win}/{len(WINDOWS)} 个窗口（全样本，选股忠实）")


if __name__ == "__main__":
    main()
