"""
③止损价「2根5分钟K确认」 跨样本/跨期 验证（防过拟合）
================================================
纯卖出逻辑改动（不动候选/评分）→ 共享 ctx。
对照：现行(单次触碰 STOP_CONFIRM_BARS=1) vs 2根5分钟K确认(=2)。
目的：减少次日早盘单次插针把保本/移动止损洗出，又不放大亏损。

固定实盘口径：max_pos=4 · 分批12% · Top8 · 粘合→盈亏比 · 10%锁5% · 滑点0.1% · 封顶100。
用法: python -m backtest.validate_stopconfirm
"""
from __future__ import annotations

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import monitor.intraday as IT
from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
SLIP = 0.001
FULL = ("2025-05-22", "2026-06-02")
H1   = ("2025-05-22", "2025-11-21")
H2   = ("2025-11-24", "2026-06-02")


def _subset(ctx, codes):
    cs = set(codes)
    return {k: ({c: v for c, v in ctx[k].items() if c in cs} if k != "mk" else ctx[k])
            for k in ctx}


def _row(ctx, names, period, confirm_bars):
    IT.STOP_CONFIRM_BARS = confirm_bars
    t, ec, pos = simulate(ctx, names, period[0], period[1], max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk")
    IT.STOP_CONFIRM_BARS = 1
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只 | 固定:4仓·分批12%·Top8·滑点0.1%\n")

    random.seed(42)
    sh = list(names); random.shuffle(sh)
    half = len(sh) // 2
    A, B = sh[:half], sh[half:]

    tests = [
        ("半样本A·全期", A, FULL),
        ("半样本B·全期", B, FULL),
        ("全样本·上半段", list(names), H1),
        ("全样本·下半段", list(names), H2),
    ]

    win = 0
    for label, codeset, period in tests:
        c = _subset(ctx, codeset)
        m0 = _row(c, names, period, 1)   # 现行：单次触碰
        m1 = _row(c, names, period, 2)   # 2根确认
        better = m1["calmar"] >= m0["calmar"]
        win += 1 if better else 0
        print(f"── {label} ({period[0]}~{period[1]})")
        print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
        for nm, m in (("现行(单次触碰)", m0), ("2根5min确认", m1)):
            print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}")
        print(f"   → 2根确认 {'≥' if better else '<'} 现行 (卡玛{m1['calmar']} vs {m0['calmar']})\n")

    print("=" * 60)
    print(f"2根确认 ≥ 现行：{win}/4 个子集")
    print("4/4 → 稳健可上线；2-3/4 → 可疑；≤1 → 噪音，否决")


if __name__ == "__main__":
    main()
