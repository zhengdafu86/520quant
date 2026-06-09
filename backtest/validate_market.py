"""
大盘过滤口径对照 — 全样本+多窗口（看放松能否抓回Q4踏空的反弹）
================================================
现行: MA20>MA60(金叉)才建仓——调整后回升时反应慢、踏空(如Q4春)。
对照: slope(MA20斜率转正即放行,更灵敏) / either(金叉或转正) / always(无过滤)。
含防御板块排除(与线上一致)。固定:4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%。
用法: python3 -B -m backtest.validate_market
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
WINDOWS = [("全期", "2025-05-22", "2026-06-02"), ("Q1夏", "2025-05-22", "2025-08-31"),
           ("Q2秋", "2025-09-01", "2025-11-30"), ("Q3冬", "2025-12-01", "2026-02-28"),
           ("Q4春", "2026-03-01", "2026-06-02")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务", "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
MODES = [("现行(金叉)", "ma20_ma60"), ("斜率转正", "slope"),
         ("金叉或转正", "either"), ("无过滤", "always")]


def _row(ctx, s, e, mode):
    t, ec, pos = simulate(ctx, {c: c for c in ctx["daily"]}, s, e, max_pos=4, capital=CAP,
                          slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk", market_mode=mode)
    return metrics(t, ec, pos, CAP, ctx["mk"])


def main():
    ctx = load_ctx(ids.all_codes("5m"))
    ctx = {**ctx, "cand": {c: ({} if SM.get(c, "") in DEF else cm) for c, cm in ctx["cand"].items()}}
    print(f"有效 {len(ctx['daily'])} 只\n")
    for lab, s, e in WINDOWS:
        print(f"── {lab}")
        print(f"   {'大盘过滤':<12}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
        for nm, mode in MODES:
            m = _row(ctx, s, e, mode)
            print(f"   {nm:<12}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}")
        print()
    print("看放松大盘过滤：牛/平市少踏空(收益↑) vs 熊市多挨揍(回撤↑)的权衡。")


if __name__ == "__main__":
    main()
