"""
RS-override 验证 — 大盘弱时不空仓, 改为只放行RS极强的龙头+降仓, 看能否救回Q4踏空
================================================
现行: 大盘弱(MA20<MA60)→空仓。RS-override: 大盘弱→降到2仓+只放行 RS(个股20日−沪深300)≥阈值 的强势股。
赌V反弹时龙头先于指数起涨。连续运行+季度delta, 重点看Q4 & 全期是否不伤。
用法: python3 -B -m backtest.validate_rsoverride
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
S, E = "2025-05-22", "2026-06-02"
BOUNDS = [("Q1夏", "2025-08-31"), ("Q2秋", "2025-11-30"),
          ("Q3冬", "2026-02-28"), ("Q4春", "2026-06-02")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
# (标签, rs_override阈值)  0=现行(弱市空仓)
MODES = [("现行(弱市空仓)", 0.0), ("RS≥+10pp·弱市2仓", 0.10), ("RS≥+15pp·弱市2仓", 0.15)]


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

    res = {}
    print(f"   {'方案':<20}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
    for nm, rs in MODES:
        t, ec, pos = simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                              top_n=8, atr_mult=0.0, scale_pct=12.0,
                              priority="squeeze_risk", rs_override=rs, rs_weak_cap=2)
        m = metrics(t, ec, pos, CAP, mk); res[nm] = ec
        print(f"   {nm:<20}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)

    print(f"\n【分季当季收益(连续)】")
    print(f"   {'季度':<8}{'现行%':>9}{'RS+10%':>9}{'RS+15%':>9}")
    pa = pb = pc = CAP

    def eq_at(ec, d):
        v = CAP
        for x, e in ec:
            if x <= d: v = e
            else: break
        return v
    keys = [m[0] for m in MODES]
    for lab, b in BOUNDS:
        ea = eq_at(res[keys[0]], b); eb = eq_at(res[keys[1]], b); ec2 = eq_at(res[keys[2]], b)
        print(f"   {lab:<8}{(ea/pa-1)*100:>9.1f}{(eb/pb-1)*100:>9.1f}{(ec2/pc-1)*100:>9.1f}")
        pa, pb, pc = ea, eb, ec2
    print("\n判读: Q4转正/改善 且 全期不掉 ⇒ RS-override救回踏空,成功; 全期掉 ⇒ 弱市强势股也接飞刀,否决。")


if __name__ == "__main__":
    main()
