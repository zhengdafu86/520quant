"""
2024亏损解剖 — 逐月盈亏 + 出场分布 + Q4(924暴涨)的whipsaw特征
用法: BT_DAILY_BARS=700 python3 -B -m backtest.diagnose_2024
"""
from __future__ import annotations

import sys
import json
import statistics as st
from collections import defaultdict, Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
S, E = "2024-01-02", "2024-12-31"
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def main():
    codes = ids.all_codes("5m")
    print("载数据(缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 700)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    m5_map, day_total, sdates = load_5m_window(list(daily_map), S, E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    tdays = sorted(set(mk[(mk["d"] >= S) & (mk["d"] <= E)]["d"]))
    tidx = {d: i for i, d in enumerate(tdays)}

    trades, ec, pos = simulate(ctx, {c: c for c in daily_map}, S, E, max_pos=4, capital=CAP,
                               slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0,
                               priority="squeeze_risk")
    m = metrics(trades, ec, pos, CAP, mk)
    print(f"\n2024全年: 收益{m['ret']:+.1f}% 回撤{m['mdd']:.1f}% 交易{m['trades']} 胜率{m['win']}% | 出场{m['exits']}\n")

    # 逐月
    print("【逐月盈亏】")
    mon = defaultdict(lambda: {"pnl": 0.0, "n": 0, "w": 0})
    for t in trades:
        k = t["sell_date"][:7]
        mon[k]["pnl"] += t["pnl"]; mon[k]["n"] += 1; mon[k]["w"] += (t["pnl"] > 0)
    for k in sorted(mon):
        v = mon[k]
        print(f"  {k}: 盈亏{v['pnl']:>+9,.0f}元  {v['n']}笔  胜率{v['w']/v['n']*100:.0f}%")

    # Q4 (924) 解剖
    q4 = [t for t in trades if "2024-09-24" <= t["sell_date"] <= "2024-12-31" or
          "2024-09-24" <= t["buy_date"] <= "2024-12-31"]
    print(f"\n【Q4暴涨段(924后) {len(q4)}笔解剖】")
    holds = []
    for t in q4:
        if t["buy_date"] in tidx and t["sell_date"] in tidx:
            holds.append(tidx[t["sell_date"]] - tidx[t["buy_date"]])
    pnls = [t["pnl_pct"] for t in q4 if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]; loss = [p for p in pnls if p <= 0]
    print(f"  胜率 {len(wins)}/{len(pnls)} = {len(wins)/len(pnls)*100:.0f}%")
    print(f"  平均持有 {st.mean(holds):.1f}交易日 | ≤2日就出场 {sum(1 for h in holds if h<=2)}笔({sum(1 for h in holds if h<=2)/len(holds)*100:.0f}%)")
    print(f"  赢单均盈 {st.mean(wins) if wins else 0:+.1f}% | 亏单均亏 {st.mean(loss) if loss else 0:+.1f}%")
    print(f"  出场分布: {Counter(t['exit'] for t in q4)}")
    # 同股反复买卖(churn)
    bycode = Counter(t["code"] for t in q4)
    repeat = {c: n for c, n in bycode.items() if n >= 2}
    print(f"  同股反复进出(≥2次): {len(repeat)}只, 例: {dict(list(repeat.items())[:5])}")
    print("\n看: Q4若'持有短+亏单多+反复进出'=暴涨里被whipsaw(买回踩→急涨没踩稳→止损), 验证V型死穴。")


if __name__ == "__main__":
    main()
