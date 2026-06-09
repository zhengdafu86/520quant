"""
方案A验证 — 520进场 + MACD柱动能确认 是否提升（全1055只·分时间段跑，省内存）
================================================
对照: off(现行) / up(柱拐头向上) / trough(缩转增,更严)。MACD确认=进场前置过滤。
含防御板块排除(与线上一致)。固定: 4仓·分批12%·Top8·粘合→盈亏比·5%振幅·松锁利·滑点0.1%。

内存策略：日线+候选只计算一次(全市场,占用小)；每个时段只载入该段5分钟(占大头)，
跑完即释放。全期连续(需全部5m)在16G机上会OOM，故按4季度独立跑——off/up/trough
的相对增量在每季内仍是同宇宙对照，结论可信。
用法: python3 -B -m backtest.validate_macd   (无需BT_NO_CACHE/采样)
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
# 按年：全周期连续跑（最准——连续复利、满样本）。1055只全部5m约4G，干净环境下可放下。
WINDOWS = [("全年", "2025-05-22", "2026-06-02")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
MODES = [("现行(off)", "off"), ("柱拐头(up)", "up"), ("缩转增(trough)", "trough")]


def build_daily_cands(codes):
    """日线 + 候选 一次性算好（内存占用小，全程复用）。防御板块的候选置空。"""
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
    """只载入 [s,e] 区间的5分钟，并派生 day_total / sdates。"""
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
        print(f"   {'方案':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
        for nm, mode in MODES:
            t, ec, pos = simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=0.001,
                                  top_n=8, atr_mult=0.0, scale_pct=12.0,
                                  priority="squeeze_risk", macd_confirm=mode)
            m = metrics(t, ec, pos, CAP, mk)
            print(f"   {nm:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)
        print()
        del m5_map, day_total, sdates, ctx
        gc.collect()
    print("看MACD确认: 真提升(收益/胜率/卡玛↑) 还是 只是漏单(交易↓而单笔没变好)。")


if __name__ == "__main__":
    main()
