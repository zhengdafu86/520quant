"""
线上最新逻辑 完整回测报告 + 逐笔交易导出
================================================
口径=线上现行：4仓·分批12%·Top8·粘合→盈亏比·5%振幅·松锁利阶梯·佣金万1·T+1
                + 防御板块排除(电力/高速/燃气等，与线上扫描器一致)。
输出：汇总报告 + 分季 + 信号归因 + 逐笔交易(打印 & 存 /tmp/trades_report.csv)。
用法: python3 -B -m backtest.full_report
"""
from __future__ import annotations

import sys
import csv
import json
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
FULL = ("2025-05-22", "2026-06-02")
WINDOWS = [("Q1夏", "2025-05-22", "2025-08-31"), ("Q2秋", "2025-09-01", "2025-11-30"),
           ("Q3冬", "2025-12-01", "2026-02-28"), ("Q4春", "2026-03-01", "2026-06-02")]
DEFENSIVE = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
             "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SECMAP = json.load(open("/tmp/code_sector.json", encoding="utf-8"))
CSV_OUT = "/tmp/trades_report.csv"


def _filt(ctx):
    new = {c: ({} if SECMAP.get(c, "") in DEFENSIVE else cm) for c, cm in ctx["cand"].items()}
    n = {**ctx}; n["cand"] = new
    return n


def _kind(s):
    return "粘合" if ("粘合" in s or "发散" in s) else "回踩" if "回踩" in s else "金叉" if "金叉" in s else "其他"


def _run(ctx, s, e):
    return simulate(ctx, {c: c for c in ctx["daily"]}, s, e, max_pos=4, capital=CAP,
                    slippage=0.001, top_n=8, atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk")


def main():
    print(f"加载 {len(ids.all_codes('5m'))} 只…")
    ctx = _filt(load_ctx(ids.all_codes("5m")))
    trades, ec, openpos = _run(ctx, *FULL)
    m = metrics(trades, ec, openpos, CAP, ctx["mk"])

    print("\n" + "=" * 74)
    print("  线上最新逻辑 · 全样本回测报告（含防御板块排除）")
    print("=" * 74)
    print(f"  区间 {FULL[0]}~{FULL[1]} | 本金 {CAP:,} | 滑点0.1%(现实)")
    print(f"  总收益 {m['ret']:+.1f}%  期末≈{CAP*(1+m['ret']/100):,.0f}元 | 沪深300 {m['bench']:+.1f}% | 超额 {m['alpha']:+.1f}%")
    print(f"  最大回撤 {m['mdd']:.1f}% | 卡玛 {m['calmar']} | 交易 {m['trades']}笔 | 胜率 {m['win']}% | 盈亏比 {m['pnl_ratio']}")
    print(f"  期末未平仓 {m['open_pos']}只 | 出场分布 {m['exits']}")

    print("\n  【分季表现 · 滑点0.1%】")
    for lab, s, e in WINDOWS:
        mm = metrics(*_run(ctx, s, e), CAP, ctx["mk"])
        print(f"    {lab}: 收益{mm['ret']:+.1f}% 回撤{mm['mdd']:.1f}% 卡玛{mm['calmar']} 胜率{mm['win']}% 交易{mm['trades']}")

    # 信号归因
    posg = defaultdict(lambda: {"pnl": 0.0, "cost": 0.0, "sig": ""})
    for t in trades:
        k = (t["code"], t["buy_date"]); posg[k]["pnl"] += t["pnl"]; posg[k]["sig"] = t["sig"]
        if t.get("pnl_pct"): posg[k]["cost"] += t["pnl"] / (t["pnl_pct"] / 100.0)
    byk = defaultdict(list)
    for v in posg.values(): byk[_kind(v["sig"])].append(v)
    print("\n  【信号归因】")
    for kind in ("回踩", "粘合", "金叉"):
        g = byk.get(kind, [])
        if not g: continue
        w = sum(1 for v in g if v["pnl"] > 0); pn = sum(v["pnl"] for v in g)
        print(f"    {kind}: {len(g)}仓 胜率{w/len(g)*100:.0f}% 总盈亏{pn:+,.0f}元")

    # 逐笔交易导出 + 打印
    with open(CSV_OUT, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["序号", "代码", "买入日", "买入价", "卖出日", "卖出价", "盈亏(元)", "盈亏%", "信号", "出场"])
        for i, t in enumerate(sorted(trades, key=lambda x: x["buy_date"]), 1):
            wr.writerow([i, t["code"], t["buy_date"], t["buy"], t["sell_date"], t["sell"],
                         round(t["pnl"]), t["pnl_pct"], t["sig"], t["exit"]])
    print(f"\n  逐笔交易({len(trades)}笔)已存 {CSV_OUT}\n")
    print(f"  {'#':>3} {'代码':<7}{'买入日':<12}{'买价':>7}{'卖出日':<12}{'卖价':>7}{'盈亏元':>8}{'盈亏%':>7} {'信号':<6}{'出场'}")
    for i, t in enumerate(sorted(trades, key=lambda x: x["buy_date"]), 1):
        print(f"  {i:>3} {t['code']:<7}{t['buy_date']:<12}{t['buy']:>7.2f}{t['sell_date']:<12}"
              f"{t['sell']:>7.2f}{round(t['pnl']):>8}{t['pnl_pct']:>7.1f} {_kind(t['sig']):<6}{t['exit']}")


if __name__ == "__main__":
    main()
