"""
最新实盘逻辑回测
================================================
用当前线上实盘参数跑忠实分钟级回测，并做滑点敏感性 + 对比旧配置。

实盘参数：max_pos=4 · 分批止盈+12% · 精选Top8 · MA20追踪止损(不用ATR) · 佣金万1
宇宙：库内全部已回补 5 分钟股票（稳定宇宙）。

用法: python -m backtest.live_config
"""
from __future__ import annotations

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
START, END = "2025-05-22", "2026-06-02"
SAMPLE = 0      # 0=全部1055只(全样本)；>0=随机抽样
SEED = 42
PRIORITY = "score"   # 买入优先级：score=评分降序优先 / squeeze_risk=粘合→盈亏比


def run(ctx, names, label, max_pos, scale_pct, slippage, top_n=8, priority=PRIORITY):
    t, ec, pos = simulate(ctx, names, START, END, max_pos=max_pos, capital=CAP,
                          slippage=slippage, top_n=top_n, atr_mult=0.0,
                          scale_pct=scale_pct, priority=priority)
    m = metrics(t, ec, pos, CAP, ctx["mk"])
    m["_label"] = label
    return m


def main():
    codes = ids.all_codes("5m")
    if SAMPLE and 0 < SAMPLE < len(codes):
        random.seed(SEED)
        codes = random.sample(codes, SAMPLE)
        print(f"从已回补 {len(ids.all_codes('5m'))} 只随机抽样 {len(codes)} 只 (seed={SEED}, 可复现)")
    print(f"加载 {len(codes)} 只…")
    ctx = load_ctx(codes)
    valid = list(ctx["daily"].keys())
    names = {c: c for c in valid}
    print(f"有效数据 {len(valid)} 只 | 区间 {START}~{END}\n")

    print(f"买入优先级: {PRIORITY}（score=评分降序优先）\n")
    rows = []
    # 最新实盘配置（4仓 + 分批12% + 评分优先）在不同滑点下
    rows.append(run(ctx, names, "评分优先·滑点0",   4, 12.0, 0.000))
    rows.append(run(ctx, names, "评分优先·滑点0.1%", 4, 12.0, 0.001))
    rows.append(run(ctx, names, "评分优先·滑点0.2%", 4, 12.0, 0.002))
    # 对照：同配置但用旧的「粘合→盈亏比」优先级（看评分优先是否更好）
    rows.append(run(ctx, names, "盈亏比优先·滑点0.1%", 4, 12.0, 0.001, priority="squeeze_risk"))

    print("=" * 88)
    print("  最新实盘逻辑 回测结果（全样本 · 评分优先）")
    print("=" * 88)
    print(f"  {'配置':<20}{'收益%':>8}{'回撤%':>8}{'卡玛':>7}{'Alpha%':>9}"
          f"{'交易':>6}{'胜率%':>7}{'盈亏比':>7}{'未平':>5}")
    for m in rows:
        if not m:
            continue
        print(f"  {m['_label']:<20}{m['ret']:>8.1f}{m['mdd']:>8.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>9.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}{m['open_pos']:>5}")
    print("\n  出场分布（评分优先·滑点0.1%）:", rows[1].get("exits"))
    print(f"  沪深300同期基准: {rows[0].get('bench'):+.1f}%")
    print("\n⚠️ 量比按5分钟量重建/相对强弱跳过/无手动优先级——进出场时点为真实分钟级。")


if __name__ == "__main__":
    main()
