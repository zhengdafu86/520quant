"""
基准漂移核查 — 日线bars=320 vs 500，off模式收益是否不同(确认窗口覆盖敏感性)
================================================
假设: bars=320 刚好卡在回测窗口起点边缘,随"今天"前移而漂移。
做法: 5m只载一次复用; 分别用 320/500 日线构建候选,各跑一次off-sim,比收益+窗口回踩候选数。
用法: python3 -B -m backtest.compare_bars
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, _precompute_candidates
from data.fetcher import db
from data import intraday_store as ids

CAP = 200_000
S, E = "2025-05-22", "2026-06-02"
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def load_5m(codes):
    m5_map, day_total, sdates = {}, {}, {}
    for c in codes:
        bars = ids.get_bars(c, "5m")
        if not bars:
            continue
        m5 = {}
        for dt, o, h, l, cl, v in bars:
            dd = dt[:10]
            if S <= dd <= E:
                m5.setdefault(dd, []).append((dt, o, h, l, cl, v))
        if m5:
            m5_map[c] = m5
            day_total[c] = {dt: sum(float(b[5]) for b in bs) for dt, bs in m5.items()}
            sdates[c] = sorted(m5.keys())
    return m5_map, day_total, sdates


def build_cands(codes, nbars):
    daily_map, cand_map, npb = {}, {}, 0
    for c in codes:
        d = db.get(c, freq="day", bars=nbars)
        if d is None or d.empty:
            continue
        d = d.copy(); d["d"] = d["datetime"].astype(str).str[:10]
        daily_map[c] = d
        cm = {} if SM.get(c, "") in DEF else _precompute_candidates(d)
        cand_map[c] = cm
        npb += sum(1 for T, (sig, a, s) in cm.items() if "回踩" in sig and S <= T <= E)
    return daily_map, cand_map, npb


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 载5m(一次)…", flush=True)
    m5_map, day_total, sdates = load_5m(codes)
    mk = db.get_market(bars=700).copy(); mk["d"] = mk["datetime"].astype(str).str[:10]
    for nbars in (320, 500):
        daily_map, cand_map, npb = build_cands(list(m5_map), nbars)
        ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
               "day_total": day_total, "sdates": sdates, "mk": mk}
        t, ec, pos = simulate(ctx, {c: c for c in daily_map}, S, E, max_pos=4, capital=CAP,
                              slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0,
                              priority="squeeze_risk", limit_lock="off")
        m = metrics(t, ec, pos, CAP, mk)
        print(f"bars={nbars}: 收益{m['ret']:+.1f}% 卡玛{m['calmar']} 交易{m['trades']} "
              f"窗口回踩候选{npb}", flush=True)
    print("\n若 320≠500 ⇒ 确认bars覆盖敏感,应固定用500(留MA60余量); 若相等 ⇒ 漂移另有因。")


if __name__ == "__main__":
    main()
