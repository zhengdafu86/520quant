"""
RS-override 稳健性 — ①阈值扫描(看甜区宽不宽) ②逐笔拆解(看+收益是否靠个别幸运龙头)
用法: python3 -B -m backtest.validate_rsoverride_robust
"""
from __future__ import annotations

import sys
import json
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
S, E = "2025-05-22", "2026-06-02"
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def pos_level(trades):
    g = defaultdict(lambda: {"pnl": 0.0})
    for t in trades:
        g[(t["code"], t["buy_date"])]["pnl"] += t["pnl"]
    return g


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 载数据(缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 320)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    m5_map, day_total, sdates = load_5m_window(list(daily_map), S, E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    names = {c: c for c in daily_map}
    print(f"有效 {len(daily_map)} 只\n", flush=True)

    def run(rs):
        return simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                        top_n=8, atr_mult=0.0, scale_pct=12.0,
                        priority="squeeze_risk", rs_override=rs, rs_weak_cap=2)

    print("【阈值扫描】")
    print(f"   {'RS阈值':<12}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}")
    trades_by = {}
    for rs in (0.0, 0.06, 0.08, 0.10, 0.12, 0.15):
        t, ec, pos = run(rs)
        trades_by[rs] = t
        m = metrics(t, ec, pos, CAP, mk)
        lab = "现行(空仓)" if rs == 0 else f"RS≥+{rs*100:.0f}pp"
        print(f"   {lab:<12}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}", flush=True)

    # 逐笔拆解: RS+10 比 现行 多出来的"弱市override"仓
    print("\n【RS≥+10pp 逐笔拆解：弱市override多买的仓】")
    g0 = pos_level(trades_by[0.0]); g10 = pos_level(trades_by[0.10])
    extra = [(k, v["pnl"]) for k, v in g10.items() if k not in g0]
    extra.sort(key=lambda x: -x[1])
    tot = sum(p for _, p in extra)
    win = sum(1 for _, p in extra if p > 0)
    print(f"  override多买 {len(extra)} 仓 | 总盈亏 {tot:+,.0f}元 | 胜率 {win}/{len(extra)}")
    print("  贡献Top8:")
    for (code, bd), p in extra[:8]:
        print(f"    {code} {bd}  {p:+,.0f}")
    pos_c = sorted([p for _, p in extra if p > 0], reverse=True)
    if pos_c and sum(pos_c) > 0:
        print(f"  正贡献集中度: Top1占{pos_c[0]/sum(pos_c)*100:.0f}% | Top3占{sum(pos_c[:3])/sum(pos_c)*100:.0f}%")
    print("\n判读: 阈值扫描多点为正=甜区宽(稳); 仅+10一点正=过拟合。拆解Top1/3占比低=分散(稳)。")


if __name__ == "__main__":
    main()
