"""
涨停锁利 disable 增益的逐笔拆解 — 看 +delta 是集中在1-2笔(脆弱)还是分散(稳健)
================================================
跑 off 与 disable，按(代码,买入日)聚合到仓位级盈亏；
对 off 中涨停锁利(LIMIT_LOCK)出场的仓，对比其在 disable 中(改持有)的最终盈亏 → 每仓贡献。
用法: python3 -B -m backtest.diagnose_lldelta
"""
from __future__ import annotations

import sys
import json
from collections import defaultdict
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


def pos_level(trades):
    """聚合到仓位级: {(code,buy_date): {pnl, exits, sell_date}}"""
    g = defaultdict(lambda: {"pnl": 0.0, "exits": set(), "sell": ""})
    for t in trades:
        k = (t["code"], t["buy_date"])
        g[k]["pnl"] += t["pnl"]
        g[k]["exits"].add(t["exit"])
        g[k]["sell"] = max(g[k]["sell"], t["sell_date"])
    return g


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
    names = {c: c for c in daily_map}

    def run(mode):
        t, ec, pos = simulate(ctx, names, S, E, max_pos=4, capital=CAP, slippage=0.001,
                              top_n=8, atr_mult=0.0, scale_pct=12.0,
                              priority="squeeze_risk", limit_lock=mode)
        return t, metrics(t, ec, pos, CAP, mk)

    t_off, m_off = run("off")
    t_dis, m_dis = run("disable")
    print(f"off    : 收益{m_off['ret']:+.1f}% 期末{CAP*(1+m_off['ret']/100):,.0f}")
    print(f"disable: 收益{m_dis['ret']:+.1f}% 期末{CAP*(1+m_dis['ret']/100):,.0f}")
    delta_yuan = CAP * (m_dis['ret'] - m_off['ret']) / 100
    print(f"全局差异: {m_dis['ret']-m_off['ret']:+.1f}pp ≈ {delta_yuan:+,.0f}元\n")

    g_off, g_dis = pos_level(t_off), pos_level(t_dis)
    # off中涨停锁利的仓
    ll = [(k, v) for k, v in g_off.items() if "LIMIT_LOCK" in v["exits"]]
    print(f"off中涨停锁利仓: {len(ll)} 笔。逐笔对比(disable同一entry的最终盈亏):")
    print(f"  {'代码':<8}{'买入日':<12}{'off盈亏':>10}{'disable盈亏':>12}{'额外贡献':>10}  disable出场")
    contribs = []
    for k, v in sorted(ll, key=lambda x: -(g_dis.get(x[0], {"pnl": x[1]['pnl']})['pnl'] - x[1]['pnl'])):
        code, bd = k
        offp = v["pnl"]
        disp = g_dis.get(k, {}).get("pnl", offp)
        disex = ",".join(sorted(g_dis.get(k, {}).get("exits", {"(未持有/同)"})))
        c = disp - offp
        contribs.append(c)
        print(f"  {code:<8}{bd:<12}{offp:>10,.0f}{disp:>12,.0f}{c:>10,.0f}  {disex}")
    tot = sum(contribs)
    print(f"\n涨停锁利仓 直接额外贡献合计: {tot:+,.0f}元 (占全局差异{delta_yuan:+,.0f}元的 {tot/delta_yuan*100 if delta_yuan else 0:.0f}%)")
    pos_c = sorted([c for c in contribs if c > 0], reverse=True)
    if pos_c:
        top1 = pos_c[0] / sum(pos_c) * 100
        top3 = sum(pos_c[:3]) / sum(pos_c) * 100
        print(f"正贡献集中度: Top1占{top1:.0f}% | Top3占{top3:.0f}% | 正贡献{len(pos_c)}笔/共{len(contribs)}笔")
        print("判读: Top1/Top3占比高(如Top1>50%)=脆弱(靠个别幸运单); 分散=稳健,可上线。")


if __name__ == "__main__":
    main()
