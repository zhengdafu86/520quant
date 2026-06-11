"""
强弱自动减仓 回测(代理版) — 按"指数趋势+两融"日度强弱自动调持仓数上限 vs 固定4仓
================================================
⚠️ 代理版: 缺"涨跌家数"(无历史)，仅用 指数(金叉/MA20斜率) + 两融余额5日变化 算强弱verdict。
verdict: 强/中性→4仓, 弱→cap_weak(测1仓 / 0仓)。连续运行+季度delta。
预期: 大概率跑输固定4仓(机械按弱降仓=踏空回升,已被tiered/cross_slope验证)。
用法: python3 -B -m backtest.validate_strengthcap
"""
from __future__ import annotations

import sys
import json
import requests
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


def fetch_margin():
    """东财全市场融资余额日历史 → 有序 [(date, rzye)]。"""
    u = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    p = {"reportName": "RPTA_RZRQ_LSHJ", "columns": "DIM_DATE,RZYE",
         "sortColumns": "DIM_DATE", "sortTypes": "-1", "pageSize": "400",
         "source": "WEB", "client": "WEB"}
    r = requests.get(u, params=p, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
    d = (r.get("result") or {}).get("data") or []
    rows = [(x["DIM_DATE"][:10], float(x["RZYE"])) for x in d]
    rows.sort()
    return rows


def build_capmap(mk, margin_rows, cap_weak):
    """对每个交易日: 指数(金叉/斜率)+两融5日变化 → 强弱 → 持仓上限。"""
    mdates = [d for d, _ in margin_rows]
    mvals = {d: v for d, v in margin_rows}
    import bisect
    cap_map = {}
    mk = mk.copy()
    dates = mk["d"].tolist()
    for i in range(1, len(dates)):
        T = dates[i]
        last = mk.iloc[i - 1]            # 截至T-1
        try:
            cross = float(last["ma20"]) > float(last["ma60"])
            slope = float(last.get("ma20_slope", 0))
        except Exception:
            cap_map[T] = 4; continue
        score = (1 if cross else -1) + (1 if slope > 0.001 else (-1 if slope < -0.001 else 0))
        # 两融5日变化(截至 ≤T-1 的最近披露日)
        dprev = dates[i - 1]
        j = bisect.bisect_right(mdates, dprev) - 1
        if j >= 5:
            chg5 = (mvals[mdates[j]] / mvals[mdates[j - 5]] - 1) * 100
            score += 1 if chg5 >= 1.0 else (-1 if chg5 <= -1.5 else 0)
        verdict = "强" if score >= 2 else ("弱" if score <= -2 else "中性")
        cap_map[T] = cap_weak if verdict == "弱" else 4
    return cap_map


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 载数据(缓存)…", flush=True)
    daily_map, cand_map, mk = load_daily_cands(codes, 320)
    cand_map = {c: ({} if SM.get(c, "") in DEF else v) for c, v in cand_map.items()}
    m5_map, day_total, sdates = load_5m_window(list(daily_map), S, E)
    ctx = {"daily": daily_map, "m5": m5_map, "cand": cand_map,
           "day_total": day_total, "sdates": sdates, "mk": mk}
    names = {c: c for c in daily_map}
    margin = fetch_margin()
    print(f"有效 {len(daily_map)} 只 | 两融历史 {len(margin)} 日 ({margin[0][0]}~{margin[-1][0]})\n", flush=True)

    def run(cap_map):
        return simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                        top_n=8, atr_mult=0.0, scale_pct=12.0,
                        priority="squeeze_risk", cap_map=cap_map)

    cm1 = build_capmap(mk[(mk["d"] >= S) & (mk["d"] <= E)], margin, 1)
    cm0 = build_capmap(mk[(mk["d"] >= S) & (mk["d"] <= E)], margin, 0)
    weak_days = sum(1 for v in cm1.values() if v < 4)
    print(f"判弱天数(降仓): {weak_days}/{len(cm1)}\n")

    res = {}
    MODES = [("固定4仓(现行)", None), ("弱市降1仓", cm1), ("弱市降0仓(空仓)", cm0)]
    print(f"   {'方案':<18}{'收益%':>8}{'回撤%':>7}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}")
    for nm, cmp in MODES:
        t, ec, pos = run(cmp)
        m = metrics(t, ec, pos, CAP, mk); res[nm] = ec
        print(f"   {nm:<18}{m['ret']:>8.1f}{m['mdd']:>7.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}", flush=True)

    print(f"\n【分季当季收益(连续)】")
    print(f"   {'季度':<8}{'固定4仓%':>9}{'降1仓%':>9}{'降0仓%':>9}")
    pa = pb = pc = CAP

    def eq_at(ec, d):
        v = CAP
        for x, e in ec:
            if x <= d: v = e
            else: break
        return v
    for lab, b in BOUNDS:
        ea = eq_at(res["固定4仓(现行)"], b); eb = eq_at(res["弱市降1仓"], b); ec2 = eq_at(res["弱市降0仓(空仓)"], b)
        print(f"   {lab:<8}{(ea/pa-1)*100:>9.1f}{(eb/pb-1)*100:>9.1f}{(ec2/pc-1)*100:>9.1f}")
        pa, pb, pc = ea, eb, ec2
    print("\n判读: 降仓收益≥固定 ⇒ 强弱自动减仓有效; 跑输 ⇒ 又是踏空(代理版,缺宽度仅供参考)。")


if __name__ == "__main__":
    main()
