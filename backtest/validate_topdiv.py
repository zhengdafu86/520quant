"""
方案B验证 — 在现行完整体系上，加 MACD顶背离减仓 是否提升（边际test）
================================================
隔离原则: 其余全部不动(保本/锁利/分批12%/浮盈回落/跌破MA20/硬止损都在)，只切顶背离开关。
对照: off(现行) / half(顶背离减半仓) / full(顶背离清仓)。
全年连续跑(最准)，全1055只。固定: 4仓·分批12%·Top8·粘合→盈亏比·5%振幅·松锁利·滑点0.1%·含防御排除。
用法: python3 -B -m backtest.validate_topdiv
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
WINDOWS = [("全年", "2025-05-22", "2026-06-02")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
MODES = [("现行(off)", "off"), ("顶背离减半(half)", "half"), ("顶背离清仓(full)", "full")]


def build_daily_cands(codes):
    daily_map, cand_map = {}, {}
    for c in codes:
        d = db.get(c, freq="day", bars=320)
        if d is None or d.empty:
            continue
        d = d.copy()
        d["d"] = d["datetime"].astype(str).str[:10]
        daily_map[c] = d
        cand_map[c] = {} if SM.get(c, "") in DEF else _precompute_candidates(d)
    mk = db.get_market(bars=320).copy()
    mk["d"] = mk["datetime"].astype(str).str[:10]
    return daily_map, cand_map, mk


def load_5m_window(codes, s, e):
    m5_map, day_total, sdates = {}, {}, {}
    for c in codes:
        bars = ids.get_bars(c, "5m")
        if not bars:
            continue
        m5 = {}
        for dt, o, h, l, cl, v in bars:
            dd = dt[:10]
            if s <= dd <= e:
                m5.setdefault(dd, []).append((dt, o, h, l, cl, v))
        if not m5:
            continue
        m5_map[c] = m5
        day_total[c] = {dt: sum(float(b[5]) for b in bs) for dt, bs in m5.items()}
        sdates[c] = sorted(m5.keys())
    return m5_map, day_total, sdates


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 计算日线+候选(一次)…", flush=True)
    daily_map, cand_map, mk = build_daily_cands(codes)
    print(f"有效 {len(daily_map)} 只\n", flush=True)
    names = {c: c for c in daily_map}

    for lab, s, e in WINDOWS:
        m5_map, day_total, sdates = load_5m_window(list(daily_map), s, e)
        ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
               "day_total": day_total, "sdates": sdates, "mk": mk}
        print(f"── {lab} {s}~{e}  (载入5m {len(m5_map)}只)")
        print(f"   {'方案':<18}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
        for nm, mode in MODES:
            t, ec, pos = simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=0.001,
                                  top_n=8, atr_mult=0.0, scale_pct=12.0,
                                  priority="squeeze_risk", top_div=mode)
            m = metrics(t, ec, pos, CAP, mk)
            ndiv = sum(1 for x in t if "TOP_DIV" in x.get("exit", ""))
            print(f"   {nm:<18}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}   顶背离触发{ndiv}次", flush=True)
        print()
        del m5_map, day_total, sdates, ctx
        gc.collect()
    print("看顶背离: 真提升(收益/卡玛↑或回撤↓) vs 误杀(收益↓)。触发次数=信号活跃度。")


if __name__ == "__main__":
    main()
