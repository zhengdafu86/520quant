"""
仓位利用率测量：到底有多少时间是满仓(4/4)？
================================================
跑现行实盘配置，从成交记录重建每日收盘持仓数，统计：
平均持仓数、各持仓档(0~4)天数占比、满仓占比。回答"是不是一直满仓、会不会错过优质股"。
固定:全样本·4仓·分批12%·Top8·粘合→盈亏比·滑点0.1%·封顶100。
用法: python -m backtest.measure_slots
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
START, END = "2025-05-22", "2026-06-02"
MAXPOS = 4


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    print(f"有效 {len(names)} 只\n")

    trades, ec, openpos = simulate(ctx, names, START, END, max_pos=MAXPOS, capital=CAP,
                                   slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0,
                                   priority="squeeze_risk")
    days = [d for d, _ in ec]
    last_day = days[-1] if days else END

    # 每个持仓 = (code, buy_date)，占坑区间 [buy_date, 最后一次卖出日)；末仓占到末日
    groups = {}
    for t in trades:
        groups.setdefault((t["code"], t["buy_date"]), []).append(t["sell_date"])
    intervals = [(bd, max(sds)) for (code, bd), sds in groups.items()]
    for code, p in openpos.items():
        intervals.append((p["entry_date"], None))   # 未平仓：占到末日

    # 每个交易日收盘持仓数：buy_date <= day < sell_date（卖出日盘中已平，收盘不计）
    counts = []
    for day in days:
        cnt = 0
        for bd, sd in intervals:
            if bd <= day and (sd is None or day < sd):
                cnt += 1
        counts.append(min(cnt, MAXPOS))   # 防同日换手瞬时溢出

    dist = Counter(counts)
    n = len(counts) or 1
    avg = sum(counts) / n
    m = metrics(trades, ec, openpos, CAP, ctx["mk"])

    print(f"区间 {START}~{END} | 交易日 {n} 天 | 收益{m['ret']}% 回撤{m['mdd']}% 卡玛{m['calmar']}")
    print(f"平均持仓 {avg:.2f}/{MAXPOS} 仓\n")
    print("持仓档分布（按收盘持仓数）:")
    for k in range(MAXPOS + 1):
        d = dist.get(k, 0)
        bar = "█" * int(d / n * 40)
        print(f"  {k}仓: {d:>3}天 ({d/n*100:>4.1f}%) {bar}")
    full = dist.get(MAXPOS, 0)
    print(f"\n满仓({MAXPOS}/{MAXPOS}) 占比: {full/n*100:.1f}%  ← 越高=越可能错过后来的优质股")
    print(f"有空位(<{MAXPOS}仓) 占比: {(n-full)/n*100:.1f}%  ← 这些时段新优质股能进")


if __name__ == "__main__":
    main()
