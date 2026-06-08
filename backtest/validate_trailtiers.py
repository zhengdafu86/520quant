"""
追踪止损阶梯对照 跨样本/跨期 验证
================================================
现行: >5%保本 / >10%锁+5% / >20%锁+13% / >30%锁+20%
提案: >5%锁+5% / >10%锁+7% / >15%锁+11% / >20%锁+15%   (更密集锁利)
纯卖出改动（不动候选/评分）→ 共享 ctx。
固定实盘口径：max_pos=4 · 分批12% · Top8 · 粘合→盈亏比 · 滑点0.1% · 封顶100。
用法: python -m backtest.validate_trailtiers
"""
from __future__ import annotations

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
SLIP = 0.001
FULL = ("2025-05-22", "2026-06-02")
H1   = ("2025-05-22", "2025-11-21")
H2   = ("2025-11-24", "2026-06-02")

TIERS_CUR  = [(30.0, "+20%", 1.20), (20.0, "+13%", 1.13), (10.0, "+5%", 1.05), (5.0, "保本", 1.002)]
TIERS_USER = [(20.0, "+15%", 1.15), (15.0, "+11%", 1.11), (10.0, "+7%", 1.07), (5.0, "+5%", 1.05)]


def _subset(ctx, codes):
    cs = set(codes)
    return {k: ({c: v for c, v in ctx[k].items() if c in cs} if k != "mk" else ctx[k])
            for k in ctx}


def _row(ctx, names, period, tiers):
    t, ec, pos = simulate(ctx, names, period[0], period[1], max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk", tiers=tiers, ma5_min=10.0)
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
        m0 = _row(c, names, period, TIERS_CUR)
        m1 = _row(c, names, period, TIERS_USER)
        better = m1["calmar"] >= m0["calmar"]
        win += 1 if better else 0
        print(f"── {label} ({period[0]}~{period[1]})")
        print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, m in (("现行阶梯", m0), ("提案(密集锁利)", m1)):
            print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print(f"   → 提案 {'≥' if better else '<'} 现行 (卡玛{m1['calmar']} vs {m0['calmar']})\n")

    print("=" * 60)
    print(f"提案(密集锁利) ≥ 现行：{win}/4 个子集")
    print("4/4 → 稳健可上线；2-3/4 → 可疑；≤1 → 否决")


if __name__ == "__main__":
    main()
