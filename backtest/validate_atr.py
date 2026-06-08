"""
回踩「近20日ATR%弹性门槛」 全样本+多窗口验证（过滤低波动呆滞股）
================================================
假设：呆滞低波动股(高速/电力等)回踩后涨不动 → 用 近20日ATR%(平均真实波幅/股价)
过滤：ATR% < 阈值 的回踩候选剔除，只买"有弹性、走得动"的。
做法：直接按ATR%过滤现有候选(不改signal、不重算)，复用ctx缓存。
固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%。
用法: python3 -B -m backtest.validate_atr
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
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
THRESHOLDS = [0.0, 2.5, 3.0, 3.5]   # 0=现行(不过滤)


def _atr20pct(d, asof):
    s = d.iloc[:asof]
    if len(s) < 21:
        return 0.0
    h = s["high"].astype(float); l = s["low"].astype(float); c = s["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(20).mean()); px = float(c.iloc[-1])
    return atr / px * 100 if px > 0 else 0.0


def _filtered_ctx(ctx, thr):
    if thr <= 0:
        return ctx
    dm = ctx["daily"]
    new_cand = {}
    for code, cm in ctx["cand"].items():
        d = dm[code]
        kept = {dt: v for dt, v in cm.items() if _atr20pct(d, v[1]) >= thr}
        new_cand[code] = kept
    n = {**ctx}; n["cand"] = new_cand
    return n


def _row(ctx, names, s, e):
    t, ec, pos = simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=SLIP,
                          top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk")
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes); names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只\n")

    variants = [(t, _filtered_ctx(ctx, t)) for t in THRESHOLDS]
    # 每档候选数（看过滤强度）
    for t, cx in variants:
        tot = sum(len(v) for v in cx["cand"].values())
        print(f"  ATR≥{t}%: 候选总数 {tot}")
    print()

    for label, s, e in WINDOWS:
        print(f"── {label}")
        print(f"   {'门槛':<10}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for t, cx in variants:
            m = _row(cx, names, s, e)
            lab = "现行" if t == 0 else f"ATR≥{t}%"
            print(f"   {lab:<10}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print()
    print("看 ATR 门槛是否在多数窗口收益/卡玛≥现行（过滤呆滞股 vs 漏掉机会的权衡）。")


if __name__ == "__main__":
    main()
