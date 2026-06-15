"""
宽度大盘过滤验证 — 用重构的宇宙涨跌比做大盘过滤 vs 现行金叉, 看能否少踏空Q4&不伤全期
================================================
breadth: 宇宙涨跌比(滞后T-1)≥阈值才建仓(替代金叉)。cross_or_breadth: 金叉或宽度转强(更快回场)。
阈值按此宇宙校准(基线~1.5)。连续运行+季度delta, 重点Q4。
用法: python3 -B -m backtest.validate_breadth
"""
from __future__ import annotations

import sys
import os
import json
import bisect
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
S, E = "2025-05-22", "2026-06-02"
BOUNDS = [("Q1夏", "2025-08-31"), ("Q2秋", "2025-11-30"),
          ("Q3冬", "2026-02-28"), ("Q4春", "2026-06-02")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 载数据(缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 320)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    m5_map, day_total, sdates = load_5m_window(list(daily_map), S, E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    names = {c: c for c in daily_map}

    # 载入重构涨跌比 → 比值序列, 并对每个交易日滞后对齐(用 <T 的最近一天)
    bh = json.load(open(os.path.expanduser("~/.520quant/breadth_hist.json")))
    bdates = sorted(bh)
    bratio = {d: (bh[d][0] / bh[d][1] if bh[d][1] else 9.99) for d in bdates}
    mkdates = mk["d"].tolist()
    bmap = {}
    for T in mkdates:
        j = bisect.bisect_left(bdates, T) - 1   # 最近 < T 的宽度日
        if j >= 0:
            bmap[T] = bratio[bdates[j]]
    print(f"有效 {len(daily_map)} 只 | 宽度对齐 {len(bmap)} 日\n", flush=True)

    def run(mode, thr=1.0):
        return simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                        top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk",
                        market_mode=mode, breadth_map=bmap, breadth_thresh=thr)

    MODES = [("现行金叉", "ma20_ma60", 0),
             ("宽度≥1.0", "breadth", 1.0), ("宽度≥1.3", "breadth", 1.3),
             ("宽度≥1.5", "breadth", 1.5),
             ("金叉or宽度≥1.5", "cross_or_breadth", 1.5)]
    res = {}
    print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
    for nm, mode, thr in MODES:
        t, ec, pos = run(mode, thr)
        m = metrics(t, ec, pos, CAP, mk); res[nm] = ec
        print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)

    print(f"\n【分季当季收益(连续)】")
    keys = [m[0] for m in MODES]
    print("   " + "季度".ljust(7) + "".join(k[:9].rjust(10) for k in keys))

    def eq_at(ec, d):
        v = CAP
        for x, e in ec:
            if x <= d: v = e
            else: break
        return v
    prev = {k: CAP for k in keys}
    for lab, b in BOUNDS:
        row = "   " + lab.ljust(7)
        for k in keys:
            e = eq_at(res[k], b); row += f"{(e/prev[k]-1)*100:>10.1f}"; prev[k] = e
        print(row)
    print("\n判读: 宽度/金叉or宽度 在Q4转正(少踏空)且全期≥现行 ⇒ 宽度有用; 全期掉 ⇒ 又一个噪声择时。")


if __name__ == "__main__":
    main()
