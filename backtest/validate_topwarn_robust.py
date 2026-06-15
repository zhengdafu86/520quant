"""
见顶早警 N5 稳健性 — ①阈值邻域(N3-7)在2024&2025-26都扫(看甜区宽不宽) ②2024逐月看救在哪
用法: BT_DAILY_BARS=700 python3 -B -m backtest.validate_topwarn_robust
"""
from __future__ import annotations

import sys
import gc
import json
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
NS = [0, 3, 4, 5, 6, 7]


def main():
    codes = ids.all_codes("5m")
    print("载日线+候选(bars=700,缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 700)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    names = {c: c for c in daily_map}

    def runN(ctx, s, e, n):
        return simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=0.001,
                        top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk",
                        topwarn_days=n)

    trades_2024 = {}
    for plab, s, e in [("2024", "2024-01-02", "2024-12-31"),
                       ("2025-26", "2025-05-22", "2026-06-02")]:
        m5_map, day_total, sdates = load_5m_window(list(daily_map), s, e)
        ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
               "day_total": day_total, "sdates": sdates, "mk": mk}
        print(f"\n── {plab} 阈值扫描")
        print(f"   {'N日<MA20':<10}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'交易':>6}")
        for n in NS:
            t, ec, pos = runN(ctx, s, e, n)
            m = metrics(t, ec, pos, CAP, mk)
            lab = "现行" if n == 0 else f"N{n}"
            print(f"   {lab:<10}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}{m['trades']:>6}", flush=True)
            if plab == "2024" and n in (0, 5):
                trades_2024[n] = t
        del m5_map, day_total, sdates, ctx; gc.collect()

    # 2024逐月: 现行 vs N5 救在哪
    print("\n── 2024逐月盈亏: 现行 vs N5(救在哪)")
    def bymon(trades):
        d = defaultdict(float)
        for t in trades:
            d[t["sell_date"][:7]] += t["pnl"]
        return d
    m0, m5 = bymon(trades_2024[0]), bymon(trades_2024[5])
    print(f"   {'月':<9}{'现行':>10}{'N5':>10}{'差(救)':>10}")
    for k in sorted(set(m0) | set(m5)):
        print(f"   {k:<9}{m0.get(k,0):>+10,.0f}{m5.get(k,0):>+10,.0f}{m5.get(k,0)-m0.get(k,0):>+10,.0f}")
    print("\n判读: 邻域N4/5/6都'救2024不伤2025-26'=甜区宽稳; 仅N5好=过拟合。救集中6月=对症'慢见顶'。")


if __name__ == "__main__":
    main()
