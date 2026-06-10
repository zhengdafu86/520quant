"""
开盘缓冲验证 — 开盘前N根5分钟不执行止损(让跳空企稳) vs 现行(开盘即止损)
================================================
对照: 0(现行) / 3根(≈9:45后才止损) / 6根(≈10:00后)。只延迟止损,止盈不受限,其余不动。
全年连续,全1055只(缓存)。固定: 4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%+防御排除。
用法: python3 -B -m backtest.validate_openbuffer
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
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
MODES = [("现行(开盘即止损)", 0), ("缓冲到≈9:45(3根)", 3), ("缓冲到≈10:00(6根)", 6)]


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
    print(f"   {'方案':<20}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
    for nm, buf in MODES:
        t, ec, pos = simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                              top_n=8, atr_mult=0.0, scale_pct=12.0,
                              priority="squeeze_risk", open_buffer=buf)
        m = metrics(t, ec, pos, CAP, mk)
        print(f"   {nm:<20}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)
    print("\n判读: 缓冲后收益↑或回撤↓⇒开盘止损太急,该加缓冲; 收益↓或回撤↑⇒跳空确实弱,现行对。")


if __name__ == "__main__":
    main()
