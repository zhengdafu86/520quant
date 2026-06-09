"""
按信号类型(粘合/回踩/金叉)拆分交易归因
================================================
跑线上配置回测，统计每类买点：建仓数、胜率、平均收益%、总盈亏(元)、贡献占比。
回答"买了几只粘合、收益怎么样"。复用ctx缓存。
用法: python3 -B -m backtest.measure_signals
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000


def _kind(sig: str) -> str:
    if "粘合" in sig or "发散" in sig:
        return "粘合发散"
    if "回踩" in sig:
        return "回踩"
    if "金叉" in sig:
        return "金叉"
    return "其他"


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只…")
    ctx = load_ctx(codes); names = {c: c for c in ctx["daily"]}
    trades, ec, openpos = simulate(ctx, names, "2025-05-22", "2026-06-02",
                                   max_pos=4, capital=CAP, slippage=0.001, top_n=8,
                                   atr_mult=0.0, scale_pct=12.0, priority="squeeze_risk")
    m = metrics(trades, ec, openpos, CAP, ctx["mk"])
    print(f"全期 收益{m['ret']}% 卡玛{m['calmar']} 总交易{m['trades']}笔\n")

    # 按(code,buy_date)聚合为"一个持仓"，合并分批卖出
    # 成本额由 pnl/pnl_pct 还原（trade无shares字段），用于正确算持仓收益%
    pos = defaultdict(lambda: {"pnl": 0.0, "sig": "", "cost": 0.0})
    for t in trades:
        k = (t["code"], t["buy_date"])
        pos[k]["pnl"] += t["pnl"]
        pos[k]["sig"] = t["sig"]
        if t.get("pnl_pct"):
            pos[k]["cost"] += t["pnl"] / (t["pnl_pct"] / 100.0)
    # 已实现持仓按信号分组
    grp = defaultdict(list)
    for k, v in pos.items():
        grp[_kind(v["sig"])].append(v)
    # 未平仓(持有到末)按信号计数
    open_kind = defaultdict(int)
    for code, p in openpos.items():
        open_kind[_kind(p["sig"])] += 1

    print(f"{'信号类型':<10}{'建仓数':>6}{'胜率%':>7}{'平均收益%':>9}{'总盈亏(元)':>12}{'未平仓':>6}")
    tot_pnl = sum(v["pnl"] for v in pos.values())
    for kind in ("粘合发散", "回踩", "金叉", "其他"):
        g = grp.get(kind, [])
        if not g and not open_kind.get(kind):
            continue
        n = len(g)
        wins = sum(1 for v in g if v["pnl"] > 0)
        pnl_sum = sum(v["pnl"] for v in g)
        # 平均收益%：每仓 pnl/成本额 的均值
        pcts = [v["pnl"] / v["cost"] * 100 for v in g if v["cost"] > 0]
        avg_pct = sum(pcts) / len(pcts) if pcts else 0
        wr = wins / n * 100 if n else 0
        print(f"{kind:<10}{n:>6}{wr:>7.1f}{avg_pct:>9.2f}{pnl_sum:>12,.0f}{open_kind.get(kind,0):>6}")
    print(f"\n已平仓持仓数 {len(pos)} | 总实现盈亏 {tot_pnl:,.0f} 元 | 期末未平仓 {len(openpos)} 只")
    print("注：平均收益%为每仓口径(单笔仓位算术平均)；总盈亏含分批止盈合并。")


if __name__ == "__main__":
    main()
