"""
涨停锁利 disable 增益的分时段稳健性 — 连续运行,看每季净值delta(off vs disable)
================================================
全年连续跑 off 与 disable，取季度边界净值，算各季【当季连续收益】与【disable−off差】。
若增益集中在某一季(如Q1夏)、其余季≈0或负 ⇒ regime依赖、不稳; 各季都正 ⇒ 稳健。
用法: python3 -B -m backtest.validate_ll_byq
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
BOUNDS = [("Q1夏", "2025-08-31"), ("Q2秋", "2025-11-30"),
          ("Q3冬", "2026-02-28"), ("Q4春", "2026-06-02")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 载数据…", flush=True)
    daily_map, cand_map = {}, {}
    for c in codes:
        d = db.get(c, freq="day", bars=320)
        if d is None or d.empty:
            continue
        d = d.copy(); d["d"] = d["datetime"].astype(str).str[:10]
        daily_map[c] = d
        cand_map[c] = {} if SM.get(c, "") in DEF else _precompute_candidates(d)
    mk = db.get_market(bars=320).copy(); mk["d"] = mk["datetime"].astype(str).str[:10]
    m5_map, day_total, sdates = {}, {}, {}
    for c in daily_map:
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
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    names = {c: c for c in daily_map}

    def run(mode):
        t, ec, pos = simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                              top_n=8, atr_mult=0.0, scale_pct=12.0,
                              priority="squeeze_risk", limit_lock=mode)
        return ec

    ec_off, ec_dis = run("off"), run("disable")

    def eq_at(ec, day):
        v = CAP
        for d, e in ec:
            if d <= day:
                v = e
            else:
                break
        return v

    print(f"\n{'季度':<8}{'off当季%':>10}{'disable当季%':>14}{'差(pp)':>9}{'off累计%':>10}{'dis累计%':>10}")
    prev_off = prev_dis = CAP
    for lab, b in BOUNDS:
        eo, ed = eq_at(ec_off, b), eq_at(ec_dis, b)
        q_off = (eo / prev_off - 1) * 100
        q_dis = (ed / prev_dis - 1) * 100
        cum_off = (eo / CAP - 1) * 100
        cum_dis = (ed / CAP - 1) * 100
        print(f"{lab:<8}{q_off:>10.1f}{q_dis:>14.1f}{q_dis-q_off:>9.1f}{cum_off:>10.1f}{cum_dis:>10.1f}")
        prev_off, prev_dis = eo, ed
    print("\n判读: 差(pp)若仅某季显著、其余≈0或负 ⇒ regime依赖不稳; 多季为正 ⇒ 稳健可上线。")


if __name__ == "__main__":
    main()
