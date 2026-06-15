"""
2024全年回测 — 策略在真实跨周期(Q1熊→Q2/Q3弱→Q4暴涨924行情)的表现
================================================
线上现行口径(取消涨停锁利已在check_position生效)。日线bars=700(给2024-01的MA60预热)。
内存安全: 只载2024段5m。固定: 4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%+防御排除。
用法: BT_DAILY_BARS=700 python3 -B -m backtest.validate_2024
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import simulate, metrics, load_daily_cands, load_5m_window
from data import intraday_store as ids

CAP = 200_000
S, E = "2024-01-02", "2024-12-31"
WINDOWS = [("2024全年", "2024-01-02", "2024-12-31"),
           ("Q1熊(微盘危机)", "2024-01-02", "2024-03-29"),
           ("Q2", "2024-04-01", "2024-06-30"),
           ("Q3", "2024-07-01", "2024-09-23"),
           ("Q4暴涨(924)", "2024-09-24", "2024-12-31")]
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def _run(ctx, names, s, e):
    return simulate(ctx, names, s, e, max_pos=4, capital=CAP, slippage=0.001,
                    top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk")


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 日线bars=700+候选(可能首次~25min)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 700)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    m5_map, day_total, sdates = load_5m_window(list(daily_map), S, E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    names = {c: c for c in daily_map}
    have2024 = len(m5_map)
    print(f"有效 {len(daily_map)} 只 | 2024有5m {have2024} 只\n", flush=True)

    print(f"   {'窗口':<16}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'沪深300%':>9}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
    for lab, s, e in WINDOWS:
        m = metrics(*_run(ctx, names, s, e), CAP, mk)
        print(f"   {lab:<16}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['bench']:>9.1f}{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)
    print("\n看: 2024Q1熊市策略能否扛(大盘过滤护盾) + Q4暴涨能否跟上 + 全年跨周期卡玛/Alpha。")


if __name__ == "__main__":
    main()
