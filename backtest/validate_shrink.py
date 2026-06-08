"""
回踩「多日持续缩量」作为选股优先条件 — 跨样本/跨期 验证（防过拟合）
================================================
机制（按你的思路）：多日缩量给超大加分(+100) + 无封顶评分
  → 缩量股在 top_n 精选里被优先选入（选股层面优先缩量）；
  → 买入排序仍走默认"粘合→盈亏比"（粘合仍优先）。
该改动改变评分→top_n 选股，故对两套候选分别回测。

对照：现行(无缩量加分) vs +缩量优先入选。
固定实盘口径：max_pos=4 · 分批12% · Top8 · 粘合→盈亏比 · 10%锁5% · 滑点0.1% · 无封顶。
用法: python -m backtest.validate_shrink
"""
from __future__ import annotations

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import strategy.signal_520 as sig520
from backtest.intraday_portfolio import load_ctx, simulate, metrics, _precompute_candidates
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


def _row(ctx, names, period):
    t, ec, pos = simulate(ctx, names, period[0], period[1], max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk")          # 粘合→盈亏比(不变)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只…")
    sig520.SCORE_MAX = 10 ** 9          # 无封顶，让 +100 缩量加分能真正顶起排序
    sig520.MULTIDAY_SHRINK = False
    ctx = load_ctx(codes)              # 现行候选(无缩量加分)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只 | 重算缩量优先候选(+{sig520.MULTIDAY_SHRINK_BONUS})…")

    sig520.MULTIDAY_SHRINK = True
    cand_new = {c: _precompute_candidates(ctx["daily"][c]) for c in ctx["daily"]}
    sig520.MULTIDAY_SHRINK = False
    ctx_new = dict(ctx); ctx_new["cand"] = cand_new
    print("候选重算完成\n")

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
        m0 = _row(_subset(ctx, codeset), names, period)        # 现行
        m1 = _row(_subset(ctx_new, codeset), names, period)    # 缩量优先入选
        better = m1["calmar"] >= m0["calmar"]
        win += 1 if better else 0
        print(f"── {label} ({period[0]}~{period[1]})")
        print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
        for nm, m in (("现行", m0), ("缩量优先入选", m1)):
            print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}")
        print(f"   → 缩量优先 {'≥' if better else '<'} 现行 (卡玛{m1['calmar']} vs {m0['calmar']})\n")

    print("=" * 60)
    print(f"缩量优先入选 ≥ 现行：{win}/4 个子集")
    print("4/4 → 稳健可上线；2-3/4 → 可疑；≤1 → 噪音，否决")


if __name__ == "__main__":
    main()
