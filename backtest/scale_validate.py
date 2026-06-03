"""
分批止盈(+12%) 跨样本/跨期 稳健性验证
================================================
只有当"分批+12%"在【两个不相交随机半样本】和【期间前/后半段】都稳定优于
现行，才认定不是过拟合噪音、值得上线。加载一次、复用 ctx。

用法: python -m backtest.scale_validate
"""
from __future__ import annotations

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
FULL = ("2025-05-22", "2026-06-02")
H1   = ("2025-05-22", "2025-11-21")   # 上半段
H2   = ("2025-11-24", "2026-06-02")   # 下半段


def _subset(ctx, codes):
    cs = set(codes)
    return {k: ({c: v for c, v in ctx[k].items() if c in cs} if k != "mk" else ctx[k])
            for k in ctx}


def _row(ctx, names, period, scale):
    t, ec, pos = simulate(ctx, names, period[0], period[1], max_pos=6,
                          capital=CAP, top_n=8, scale_pct=scale)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只…")
    ctx = load_ctx(codes)
    valid = list(ctx["daily"].keys())
    print(f"有效 {len(valid)} 只\n")
    names = {c: c for c in valid}

    random.seed(42)
    shuffled = valid[:]
    random.shuffle(shuffled)
    half = len(shuffled) // 2
    A, B = shuffled[:half], shuffled[half:]
    ctxA, ctxB = _subset(ctx, A), _subset(ctx, B)

    tests = [
        ("半样本A", ctxA, FULL),
        ("半样本B", ctxB, FULL),
        ("全样本·上半段", ctx, H1),
        ("全样本·下半段", ctx, H2),
    ]
    print(f"{'子集':<16}{'方案':<10}{'收益%':>8}{'回撤%':>8}{'卡玛':>7}{'Alpha%':>8}{'胜率%':>7}")
    wins = 0
    for label, c, period in tests:
        m0 = _row(c, names, period, 0.0)     # 现行
        m1 = _row(c, names, period, 12.0)    # 分批+12%
        better = m1["calmar"] > m0["calmar"]
        wins += 1 if better else 0
        for nm, m in (("现行", m0), ("分批+12%", m1)):
            print(f"{label:<16}{nm:<10}{m['ret']:>8.1f}{m['mdd']:>8.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['win']:>7.1f}")
        print(f"   → 分批{'更优✅' if better else '更差❌'}（卡玛 {m1['calmar']} vs {m0['calmar']}）\n")

    print("=" * 60)
    print(f"分批+12% 在 {wins}/4 个独立子集上优于现行")
    print("4/4 → 稳健可上线；2-3/4 → 可疑；≤1 → 噪音，否决")


if __name__ == "__main__":
    main()
