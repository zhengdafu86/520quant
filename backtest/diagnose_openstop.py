"""
开盘硬止损机会成本诊断 — 开盘附近止损的票, 当天/次日是否反弹了?
================================================
跑全年回测, 挑出 SELL_STOP 且出场在开盘附近(前3根5分钟K, ≈9:30-9:45)的成交,
看卖出后: 当日剩余时段最高价 / 当日收盘 / 次日收盘 相对卖价 → 量化"卖在地板被甩下车"。
用法: python3 -B -m backtest.diagnose_openstop
"""
from __future__ import annotations

import sys
import json
import statistics as st
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
S, E = "2025-05-22", "2026-06-02"
EARLY_BAR = 3   # 前3根5分钟K(9:30~9:45)视为"开盘附近"
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def _s(a, lbl):
    if not a:
        print(f"  {lbl}: 0个"); return
    print(f"  {lbl}: {len(a)}个 | 均值{st.mean(a)*100:+.2f}% 中位{st.median(a)*100:+.2f}% "
          f"正比例{sum(1 for x in a if x>0.005)/len(a)*100:.0f}%")


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 载数据(缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 320)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    m5_map, day_total, sdates = load_5m_window(list(daily_map), S, E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    print(f"有效 {len(daily_map)} 只 | 跑回测…", flush=True)
    trades, ec, pos = simulate(ctx, {c: c for c in daily_map}, S, E, max_pos=4, capital=CAP,
                               slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0,
                               priority="squeeze_risk")
    m = metrics(trades, ec, pos, CAP, mk)
    print(f"全年: 收益{m['ret']:+.1f}% 卡玛{m['calmar']} | 出场分布{m['exits']}\n")

    stops = [t for t in trades if t["exit"] in ("SELL_STOP", "SELL_STOP_LOW")]
    early = [t for t in stops if t.get("bar", 99) < EARLY_BAR]
    late = [t for t in stops if t.get("bar", 99) >= EARLY_BAR]
    print(f"硬/趋势止损共 {len(stops)} 笔 | 开盘附近(前{EARLY_BAR}根) {len(early)} 笔 | 盘中其余 {len(late)} 笔\n")

    intra, dayclose, nextday = [], [], []
    for t in early:
        d = daily_map[t["code"]]
        sp = float(t["sell"]); sd = t["sell_date"]; bar = t.get("bar", 0)
        day = m5_map.get(t["code"], {}).get(sd)
        if day and sp > 0:
            after = day[bar + 1:]            # 卖出之后的当日5分钟
            if after:
                intra.append(max(float(b[4]) for b in after) / sp - 1)   # 当日剩余最高
            dayclose.append(float(day[-1][4]) / sp - 1)                   # 当日收盘
        try:
            loc = d.index.get_loc(d.index[d["d"] == sd][0])
            if loc + 1 < len(d) and sp > 0:
                nextday.append(float(d.iloc[loc + 1]["close"]) / sp - 1)
        except Exception:
            pass

    print("开盘附近止损 —— 卖出后(以卖价为基准):")
    _s(intra, "当日剩余时段最高")
    _s(dayclose, "当日收盘     ")
    _s(nextday, "次日收盘     ")
    print(f"\n判读: '当日剩余最高'正比例高+幅度大 ⇒ 频繁卖在地板被甩下车,开盘止损太急(可加开盘缓冲);")
    print("      多数继续跌(为负) ⇒ 开盘止损是对的,跳空确实弱。")


if __name__ == "__main__":
    main()
