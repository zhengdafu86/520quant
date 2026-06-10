"""
老仓拖累诊断 — 持有久但收益差(<STALE_PNL)的仓有多少, 占用多少"仓位×天"
================================================
跑全年回测(现行配置), 聚合到仓位级, 看:
  ① 持有天数分布 + 各档平均收益/胜率
  ② "长持(≥10/15/20天)且最终收益<3%" 的老仓数量 + 占用的持仓天数占比(机会成本)
判断是否值得打开⑦时间止损(老仓清坑让位)。
用法: python3 -B -m backtest.diagnose_stale
"""
from __future__ import annotations

import sys
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, _precompute_candidates
from data.fetcher import db
from data import intraday_store as ids

CAP = 200_000
S, E = "2025-05-22", "2026-06-02"
STALE_PNL = 3.0
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

    # 交易日序号(用市场日历)
    tdays = sorted(set(mk[(mk["d"] >= S) & (mk["d"] <= E)]["d"]))
    tidx = {d: i for i, d in enumerate(tdays)}

    trades, ec, pos = simulate(ctx, {c: c for c in daily_map}, S, E, max_pos=4, capital=CAP,
                               slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0,
                               priority="squeeze_risk")
    m = metrics(trades, ec, pos, CAP, mk)
    print(f"全年: 收益{m['ret']:+.1f}% 卡玛{m['calmar']} 成交{m['trades']}笔\n")

    # 聚合仓位级
    g = defaultdict(lambda: {"pnl": 0.0, "cost": 0.0, "buy": "", "sell": ""})
    for t in trades:
        k = (t["code"], t["buy_date"])
        g[k]["pnl"] += t["pnl"]
        if t.get("pnl_pct"):
            g[k]["cost"] += t["pnl"] / (t["pnl_pct"] / 100.0)
        g[k]["buy"] = t["buy_date"]
        g[k]["sell"] = max(g[k]["sell"], t["sell_date"])
    poss = []
    for k, v in g.items():
        if v["cost"] <= 0 or v["buy"] not in tidx or v["sell"] not in tidx:
            continue
        hold = tidx[v["sell"]] - tidx[v["buy"]]
        pnlp = v["pnl"] / v["cost"] * 100
        poss.append((hold, pnlp))
    N = len(poss)
    tot_holddays = sum(h for h, _ in poss)
    print(f"仓位数 {N} | 总持仓天数 {tot_holddays} (仓位×交易日)\n")

    print("【持有天数分布】")
    buckets = [("<5天", 0, 4), ("5-9天", 5, 9), ("10-14天", 10, 14),
               ("15-19天", 15, 19), ("≥20天", 20, 999)]
    print(f"  {'档':<10}{'仓数':>6}{'占比':>7}{'平均收益%':>10}{'胜率%':>7}{'占用天数':>9}")
    for lab, lo, hi in buckets:
        b = [(h, p) for h, p in poss if lo <= h <= hi]
        if not b:
            print(f"  {lab:<10}{0:>6}"); continue
        ps = [p for _, p in b]
        hd = sum(h for h, _ in b)
        print(f"  {lab:<10}{len(b):>6}{len(b)/N*100:>6.0f}%{st.mean(ps):>10.1f}"
              f"{sum(1 for x in ps if x>0)/len(ps)*100:>7.0f}{hd:>9}")

    print(f"\n【老仓(长持且收益<{STALE_PNL}%)——时间止损会清掉的对象】")
    for thr in (10, 15, 20):
        stale = [(h, p) for h, p in poss if h >= thr and p < STALE_PNL]
        sd = sum(h for h, _ in stale)
        sp = [p for _, p in stale]
        print(f"  持有≥{thr}天且收益<{STALE_PNL}%: {len(stale)}笔 ({len(stale)/N*100:.0f}%) | "
              f"占用{sd}天({sd/tot_holddays*100:.0f}%仓位时间) | "
              f"均收益{st.mean(sp) if sp else 0:+.1f}%")
    print("\n判读: 老仓占比/占用仓位时间高 ⇒ 有拖累,值得测时间止损(腾仓位); 占比低 ⇒ 不必加规则。")


if __name__ == "__main__":
    main()
