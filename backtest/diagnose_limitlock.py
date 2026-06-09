"""
涨停锁利机会成本诊断 — 我涨停就卖了，之后还涨吗？涨了多少？
================================================
跑全年回测(现行配置)，挑出 exit==LIMIT_LOCK(涨停锁利)的成交，
对每笔卖出后看日线: 次日/3日/5日/10日收盘 vs 卖出价，以及后10日最高 → 量化"少赚了多少/有没有续涨"。
固定: 4仓·分批12%·Top8·粘合→盈亏比·滑点0.1% + 防御排除。全年连续。
用法: python3 -B -m backtest.diagnose_limitlock
"""
from __future__ import annotations

import sys
import gc
import json
import statistics as st
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
    print(f"有效 {len(daily_map)} 只 | 跑回测…", flush=True)

    trades, ec, pos = simulate(ctx, {c: c for c in daily_map}, S, E, max_pos=4, capital=CAP,
                               slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0,
                               priority="squeeze_risk")
    m = metrics(trades, ec, pos, CAP, mk)
    print(f"全年: 收益{m['ret']:+.1f}% 卡玛{m['calmar']} 交易{m['trades']} | 出场分布{m['exits']}\n")

    locks = [t for t in trades if t["exit"] == "LIMIT_LOCK"]
    print(f"涨停锁利出场: {len(locks)} 笔 (占全部{len(trades)}笔的{len(locks)/max(1,len(trades))*100:.0f}%)")
    if not locks:
        print("无涨停锁利成交"); return

    f1, f3, f5, f10, fmax, fmaxdraw = [], [], [], [], [], []
    keep_up = 0
    for t in locks:
        d = daily_map[t["code"]]
        sp = float(t["sell"])
        try:
            si = d.index[d["d"] == t["sell_date"]][0]
            loc = d.index.get_loc(si)
        except Exception:
            continue
        def cl(n):
            return float(d.iloc[loc + n]["close"]) if loc + n < len(d) else None
        c1, c3, c5, c10 = cl(1), cl(3), cl(5), cl(10)
        nxt = [float(d.iloc[loc + k]["high"]) for k in range(1, 11) if loc + k < len(d)]
        if sp > 0:
            if c1 is not None: f1.append(c1 / sp - 1)
            if c3 is not None: f3.append(c3 / sp - 1)
            if c5 is not None: f5.append(c5 / sp - 1)
            if c10 is not None: f10.append(c10 / sp - 1)
            if nxt:
                mx = max(nxt) / sp - 1
                fmax.append(mx)
                if mx > 0.005:        # 卖出后还涨过 >0.5%
                    keep_up += 1

    def s(a):
        return f"均值{st.mean(a)*100:+.2f}% 中位{st.median(a)*100:+.2f}% 胜率{sum(1 for x in a if x>0)/len(a)*100:.0f}%" if a else "无"
    print(f"\n卖出后(以涨停锁利卖价为基准, 收盘口径):")
    print(f"  次日: {s(f1)}")
    print(f"  3日 : {s(f3)}")
    print(f"  5日 : {s(f5)}")
    print(f"  10日: {s(f10)}")
    print(f"\n卖出后10日内【最高价】相对卖价: {s(fmax)}")
    print(f"  → 卖出后还创出更高价(>0.5%)的: {keep_up}/{len(fmax)} ({keep_up/max(1,len(fmax))*100:.0f}%)")
    if fmax:
        big = sum(1 for x in fmax if x > 0.10)
        print(f"  → 卖出后10日内最高再涨 >10% 的: {big}/{len(fmax)} ({big/len(fmax)*100:.0f}%) [明显踏空]")
    print("\n判读: 续涨比例高+最高再涨幅大 ⇒ 涨停即卖太激进,该改; 多数卖后回落 ⇒ 锁利合理。")


if __name__ == "__main__":
    main()
