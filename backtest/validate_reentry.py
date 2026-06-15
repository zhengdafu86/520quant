"""
再入冷却验证 — 止损后N日内不回购同股, 砍churn防whipsaw
两段都跑: 2024(churn年) 能否救 + 2025-26 不伤(reactive,理论上不误杀新趋势)。
用法: BT_DAILY_BARS=700 python3 -B -m backtest.validate_reentry
"""
from __future__ import annotations

import sys
import gc
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
PERIODS = [("2024", "2024-01-02", "2024-12-31"), ("2025-26", "2025-05-22", "2026-06-02")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
MODES = [("现行(off)", 0), ("冷却3日", 3), ("冷却5日", 5), ("冷却10日", 10)]


def main():
    codes = ids.all_codes("5m")
    print("载日线+候选(bars=700,缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 700)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    names = {c: c for c in daily_map}

    for plab, s, e in PERIODS:
        m5_map, day_total, sdates = load_5m_window(list(daily_map), s, e)
        ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
               "day_total": day_total, "sdates": sdates, "mk": mk}
        print(f"\n── {plab} ({s}~{e})")
        print(f"   {'方案':<12}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
        for nm, cd in MODES:
            t, ec, pos = simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=0.001,
                                  top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk",
                                  reentry_cd=cd)
            m = metrics(t, ec, pos, CAP, mk)
            print(f"   {nm:<12}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)
        del m5_map, day_total, sdates, ctx; gc.collect()
    print("\n判读: 2024收益↑/回撤↓/胜率↑ 且 2025-26不掉 ⇒ 冷却有效砍churn; 2025-26掉 ⇒ 误挡了重新转强的票。")


if __name__ == "__main__":
    main()
