"""
熊市生存测试 — 2024Q1真实熊段(回补的5m) 检验策略（高效分段加载,不OOM）
================================================
区间: 2024-01-02~03-29(1月阴跌→2月初微盘股流动性危机崩盘见底→2-3月V反弹)。
检验: ①现行配置真熊市能不能扛(回撤/收益) ②大盘过滤(MA20>MA60)的护盾价值(对比无过滤)
      ③熊市降仓(4→2→1)有没有用。
内存策略: 日线+候选一次算好(bars=700,覆盖2024-01的MA60预热)；只载入2024熊段5m(很小)。
固定: 分批12%·Top8·粘合→盈亏比·5%振幅·松锁利·滑点0.1% + 防御板块排除。
注: /tmp/code_sector.json 仅121只(线上选中过的)，2024熊段选股多不在内→防御过滤在此偏弱。
用法: python3 -B -m backtest.validate_bear
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
BEAR_S, BEAR_E = "2024-01-02", "2024-03-29"
WINDOWS = [("全熊段", "2024-01-02", "2024-03-29"),
           ("崩盘段", "2024-01-02", "2024-02-07"),
           ("反弹段", "2024-02-08", "2024-03-29")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
# (标签, market_mode, max_pos)
VARIANTS = [("现行金叉·4仓", "ma20_ma60", 4), ("无过滤·4仓", "always", 4),
            ("现行金叉·2仓", "ma20_ma60", 2), ("现行金叉·1仓", "ma20_ma60", 1)]


def build_daily_cands(codes):
    daily_map, cand_map = {}, {}
    for c in codes:
        d = db.get(c, freq="day", bars=700)   # 700→回看到2023夏,给2024-01的MA60预热
        if d is None or d.empty:
            continue
        d = d.copy()
        d["d"] = d["datetime"].astype(str).str[:10]
        daily_map[c] = d
        cand_map[c] = {} if SM.get(c, "") in DEF else _precompute_candidates(d)
    mk = db.get_market(bars=700).copy()
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
    print(f"全市场 {len(codes)} 只 | 计算日线+候选(bars=700,一次)…", flush=True)
    daily_map, cand_map, mk = build_daily_cands(codes)
    names = {c: c for c in daily_map}
    print(f"有效 {len(daily_map)} 只 | 载入2024熊段5m…", flush=True)
    m5_map, day_total, sdates = load_5m_window(list(daily_map), BEAR_S, BEAR_E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    seg = mk[(mk["d"] >= BEAR_S) & (mk["d"] <= BEAR_E)]
    bench = (float(seg.iloc[-1]["close"]) / float(seg.iloc[0]["close"]) - 1) * 100 if len(seg) else 0
    print(f"含2024_5m {len(m5_map)}只 | 大盘2024段{len(seg)}天 | 沪深300同期 {bench:+.1f}%\n", flush=True)

    for lab, s, e in WINDOWS:
        print(f"── {lab} {s}~{e}")
        print(f"   {'方案':<14}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
        for nm, mode, mp in VARIANTS:
            t, ec, pos = simulate(ctx, names, s, e, max_pos=mp, capital=CAP, slippage=0.001,
                                  top_n=8, atr_mult=0.0, scale_pct=12.0,
                                  priority="squeeze_risk", market_mode=mode)
            m = metrics(t, ec, pos, CAP, mk)
            print(f"   {nm:<14}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
                  f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)
        print()
    print("看点: ①现行熊市回撤多大 ②无过滤vs金叉=过滤护盾价值 ③降仓能否压回撤")


if __name__ == "__main__":
    main()
