"""
Q4踏空精细修复验证 — 大盘分级放行(tiered_half) vs 现行金叉
================================================
tiered_half: 金叉满仓(4) / 金叉未到但MA20斜率转正(回升初期)半仓(2) / 斜率向下空仓(0)。
对照现行 ma20_ma60(金叉满仓/否则空仓)。看: ①全期是否保住收益(关键,之前粗放放松全期变差)
②Q4踏空是否改善。连续运行+季度边界净值delta。其余全不动。
用法: python3 -B -m backtest.validate_q4soft
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
MODES = [("现行金叉", "ma20_ma60"), ("分级放行(tiered_half)", "tiered_half")]


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 日线+候选(缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 320)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    m5_map, day_total, sdates = load_5m_window(list(daily_map), S, E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    names = {c: c for c in daily_map}
    print(f"有效 {len(daily_map)} 只\n", flush=True)

    def run(mode):
        return simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                        top_n=8, atr_mult=0.0, scale_pct=12.0,
                        priority="squeeze_risk", market_mode=mode)

    results = {}
    print(f"   {'方案':<22}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
    for nm, mode in MODES:
        t, ec, pos = run(mode)
        m = metrics(t, ec, pos, CAP, mk)
        results[mode] = ec
        print(f"   {nm:<22}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)

    print(f"\n【分季当季收益(连续)】")
    print(f"   {'季度':<8}{'现行金叉%':>10}{'分级放行%':>11}{'差(pp)':>8}")
    pa = pb = CAP

    def eq_at(ec, day):
        v = CAP
        for d, e in ec:
            if d <= day:
                v = e
            else:
                break
        return v
    for lab, b in BOUNDS:
        ea = eq_at(results["ma20_ma60"], b); eb = eq_at(results["tiered_half"], b)
        qa = (ea / pa - 1) * 100; qb = (eb / pb - 1) * 100
        print(f"   {lab:<8}{qa:>10.1f}{qb:>11.1f}{qb-qa:>8.1f}")
        pa, pb = ea, eb
    print("\n判读: 全期不掉(≥现行) 且 Q4差为正 ⇒ 精细修复成功; 全期掉了 ⇒ 又是βtrap,否决。")


if __name__ == "__main__":
    main()
