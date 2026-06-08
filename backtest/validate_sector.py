"""
剔除高股息防御板块(电力/铁路公路/燃气/公用事业) 全样本+多窗口验证
================================================
假设：这类低波动防御股回踩后涨不动、占仓位。剔除它们看是否提升。
做法：按行业过滤现有候选(不改signal/不重算)，复用ctx缓存。
固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%。
用法: python3 -B -m backtest.validate_sector
"""
from __future__ import annotations

import sys
import json
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
EXCLUDE = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
           "高速公路", "供气供热"}
SECMAP = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def _filtered(ctx, on):
    if not on:
        return ctx
    new = {c: cm for c, cm in ctx["cand"].items() if SECMAP.get(c, "") not in EXCLUDE}
    # 被排除的 code 给空候选
    for c in ctx["cand"]:
        if c not in new:
            new[c] = {}
    n = {**ctx}; n["cand"] = new
    return n


def _row(ctx, names, s, e):
    t, ec, pos = simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=SLIP,
                          top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk")
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes); names = {c: c for c in ctx["daily"]}
    excl = [c for c in ctx["cand"] if SECMAP.get(c, "") in EXCLUDE and ctx["cand"][c]]
    print(f"有效 {len(names)} 只 | 命中防御板块候选股 {len(excl)} 只\n")
    base = ctx
    filt = _filtered(ctx, True)

    for label, s, e in WINDOWS:
        print(f"── {label}")
        print(f"   {'方案':<14}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
        for nm, cx in (("现行", base), ("剔除防御板块", filt)):
            m = _row(cx, names, s, e)
            print(f"   {nm:<14}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")
        print()
    print("看剔除防御板块是否在多数窗口收益/卡玛≥现行。")


if __name__ == "__main__":
    main()
