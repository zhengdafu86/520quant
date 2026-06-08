"""
实盘参数 跨样本/跨期 稳健性验证（防过拟合）
================================================
候选改动：① 评分优先(替代 粘合→盈亏比)  ② 8%锁5%(替代 10%锁5%)
在【两个不相交半样本·全期】+【全样本·上/下半段】共4个独立子集上，
对比 现行 / +评分优先 / +评分优先+8% 三档。只有候选在多数子集上稳定不劣，
才认定不是某段行情的过拟合，值得定为实盘参数。

固定实盘口径：max_pos=4 · 分批+12% · 精选Top8 · 滑点0.1%。加载一次复用 ctx。
用法: python -m backtest.validate_live
"""
from __future__ import annotations

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
SLIP = 0.001          # 0.1% 现实滑点
FULL = ("2025-05-22", "2026-06-02")
H1   = ("2025-05-22", "2025-11-21")
H2   = ("2025-11-24", "2026-06-02")

TIERS_CUR  = [(30.0, "+20%", 1.20), (20.0, "+13%", 1.13), (10.0, "+5%", 1.05), (5.0, "保本", 1.002)]
TIERS_PROP = [(30.0, "+20%", 1.20), (20.0, "+13%", 1.13), ( 8.0, "+5%", 1.05), (5.0, "保本", 1.002)]

# (标签, priority, tiers, ma5_min)
CONFIGS = [
    ("现行(盈亏比·10%)", "squeeze_risk", TIERS_CUR,  10.0),
    ("+评分优先(·10%)",  "score",        TIERS_CUR,  10.0),
    ("+评分优先+8%",     "score",        TIERS_PROP,  8.0),
]


def _subset(ctx, codes):
    cs = set(codes)
    return {k: ({c: v for c, v in ctx[k].items() if c in cs} if k != "mk" else ctx[k])
            for k in ctx}


def _row(ctx, names, period, priority, tiers, ma5_min):
    t, ec, pos = simulate(ctx, names, period[0], period[1], max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority=priority, tiers=tiers, ma5_min=ma5_min)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只…")
    ctx = load_ctx(codes)
    valid = list(ctx["daily"].keys())
    names = {c: c for c in valid}
    print(f"有效 {len(valid)} 只 | 固定:4仓·分批12%·Top8·滑点0.1%\n")

    random.seed(42)
    shuffled = valid[:]; random.shuffle(shuffled)
    half = len(shuffled) // 2
    A, B = shuffled[:half], shuffled[half:]
    ctxA, ctxB = _subset(ctx, A), _subset(ctx, B)

    tests = [
        ("半样本A·全期", ctxA, FULL),
        ("半样本B·全期", ctxB, FULL),
        ("全样本·上半段", ctx, H1),
        ("全样本·下半段", ctx, H2),
    ]

    win_score = win_combo = 0   # 评分优先 / 评分+8% 相对现行 的卡玛胜出计数
    for label, c, period in tests:
        print(f"── {label} ({period[0]}~{period[1]}) " + "─" * 28)
        print(f"   {'方案':<18}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'胜率%':>7}{'盈亏比':>7}")
        ms = []
        for tag, pri, tiers, m5m in CONFIGS:
            m = _row(c, names, period, pri, tiers, m5m); ms.append(m)
            print(f"   {tag:<18}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        base, sc, combo = ms[0]["calmar"], ms[1]["calmar"], ms[2]["calmar"]
        win_score += 1 if sc   >= base else 0
        win_combo += 1 if combo >= base else 0
        print(f"   → 评分优先 {'≥' if sc>=base else '<'} 现行 (卡玛{sc} vs {base}) | "
              f"评分+8% {'≥' if combo>=base else '<'} 现行 (卡玛{combo} vs {base})\n")

    print("=" * 64)
    print(f"评分优先   ≥ 现行：{win_score}/4 个子集")
    print(f"评分优先+8% ≥ 现行：{win_combo}/4 个子集")
    print("4/4 → 稳健，可定为实盘参数；2-3/4 → 可疑；≤1 → 过拟合，否决")


if __name__ == "__main__":
    main()
