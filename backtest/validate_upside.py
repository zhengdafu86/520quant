"""
回踩「上方空间」过滤 — 全样本 + 多时间窗口验证（不抽样）
================================================
要求 近X日最高 ≥ 当日收盘×1.05（到近期高点≥5%空间），否则过滤(潜力不足)。
测 X=10 / X=20，对照现行(无此过滤)。改变候选合格性→重算候选。
固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%·封顶100。
用法: python -m backtest.validate_upside
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import strategy.signal_520 as sig520
from backtest.intraday_portfolio import load_ctx, simulate, metrics, _precompute_candidates
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


def _row(ctx, names, start, end):
    t, ec, pos = simulate(ctx, names, start, end, max_pos=4, capital=CAP,
                          slippage=SLIP, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk")
    return metrics(t, ec, pos, CAP, ctx["mk"])


def _build_cand(ctx, lookback):
    """以指定上方空间过滤重算候选，返回新 ctx。lookback=0 表示关闭过滤(现行)。"""
    if lookback <= 0:
        sig520.UPSIDE_ROOM_FILTER = False
    else:
        sig520.UPSIDE_ROOM_FILTER = True
        sig520.UPSIDE_LOOKBACK = lookback
        sig520.UPSIDE_MIN_ROOM = 0.05
    cand = {c: _precompute_candidates(ctx["daily"][c]) for c in ctx["daily"]}
    sig520.UPSIDE_ROOM_FILTER = False
    n = {**ctx}; n["cand"] = cand
    return n


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    sig520.UPSIDE_ROOM_FILTER = False
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只 | 重算候选(现行/X10/X20)…")
    variants = {
        "现行": ctx,
        "上方空间X50": _build_cand(ctx, 50),
        "上方空间X60": _build_cand(ctx, 60),
        "上方空间X70": _build_cand(ctx, 70),
    }
    print("重算完成\n")

    for label, start, end in WINDOWS:
        print(f"── {label} ({start}~{end})")
        print(f"   {'方案':<14}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, cx in variants.items():
            m = _row(cx, names, start, end)
            print(f"   {nm:<14}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print()
    print("=" * 60)
    print("看各窗口：上方空间过滤(X10/X20) 是否稳定优于现行。")


if __name__ == "__main__":
    main()
