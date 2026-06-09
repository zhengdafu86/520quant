"""
方案验证 — 涨停锁利处理方式 对照（全年连续,全1055只）
================================================
对照: off(现行,涨停全卖) / disable(不锁利,完全交给追踪止损) / half(涨停减半留底仓)。
只切涨停锁利这一处,其余全不动(保本/锁利阶梯/分批12%/浮盈回落/跌破MA20/硬止损都在)。
固定: 4仓·Top8·粘合→盈亏比·5%振幅·松锁利·滑点0.1% + 防御排除。
用法: python3 -B -m backtest.validate_limitlock
"""
from __future__ import annotations

import sys
import gc
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
MODES = [("现行·涨停全卖(off)", "off"), ("不锁利·交追踪止损(disable)", "disable"),
         ("涨停减半·留底仓(half)", "half")]


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 日线+候选+载5m…", flush=True)
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
    print(f"有效 {len(daily_map)} 只\n", flush=True)

    print(f"   {'涨停处理':<26}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
    for nm, mode in MODES:
        t, ec, pos = simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                              top_n=8, atr_mult=0.0, scale_pct=12.0,
                              priority="squeeze_risk", limit_lock=mode)
        m = metrics(t, ec, pos, CAP, mk)
        print(f"   {nm:<26}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)
    print("\n判读: ①/②收益或卡玛↑ ⇒ 涨停即卖太激进,该改; 都不如off ⇒ 落袋为安是对的(小样本兑现>博续涨)。")


if __name__ == "__main__":
    main()
