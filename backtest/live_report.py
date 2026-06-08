"""
线上真实配置 — 全样本完整回测报表
================================================
严格用线上部署的默认逻辑（被否决的改动一律未采纳）：
  4仓 · 分批止盈12% · 精选Top8 · 粘合→盈亏比 · MA20松锁利阶梯 ·
  5%振幅门槛 · 单日缩量门槛 · 止损单次触碰 · 评分封顶100 · 佣金万1 · T+1
全样本(全部已回补股票)。给出全期+滑点敏感+出场分布+分季窗口。
用法: python -m backtest.live_report
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 显式锁定为线上默认（防止环境残留改动）
import strategy.signal_520 as sig520
import monitor.intraday as IT
sig520.MULTIDAY_SHRINK = False
sig520.UPSIDE_ROOM_FILTER = False
sig520.SCORE_MAX = 100
IT.AMPLITUDE_CAP = 5.0
IT.STOP_CONFIRM_BARS = 1

from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAP = 200_000
FULL = ("2025-05-22", "2026-06-02")
WINDOWS = [
    ("Q1夏", "2025-05-22", "2025-08-31"),
    ("Q2秋", "2025-09-01", "2025-11-30"),
    ("Q3冬", "2025-12-01", "2026-02-28"),
    ("Q4春", "2026-03-01", "2026-06-02"),
]


def run(ctx, names, start, end, slip):
    t, ec, pos = simulate(ctx, names, start, end, max_pos=4, capital=CAP,
                          slippage=slip, top_n=8, atr_mult=0.0, scale_pct=12.0,
                          priority="squeeze_risk")
    m = metrics(t, ec, pos, CAP, ctx["mk"])
    m["_trades"] = t
    return m


def _avg_hold(trades, ec):
    # 用交易日序号估算平均持有交易日
    days = [d for d, _ in ec]
    idx = {d: i for i, d in enumerate(days)}
    hs = [idx.get(t["sell_date"], 0) - idx.get(t["buy_date"], 0)
          for t in trades if t["buy_date"] in idx and t["sell_date"] in idx]
    return sum(hs) / len(hs) if hs else 0


def main():
    codes = ids.all_codes("5m")
    print(f"加载 {len(codes)} 只（全样本）…")
    ctx = load_ctx(codes)
    names = {c: c for c in ctx["daily"]}
    n = len(names)
    print(f"有效 {n} 只 | 区间 {FULL[0]}~{FULL[1]}\n")

    print("=" * 78)
    print("  线上真实配置 · 全样本回测报表")
    print("  4仓 / 分批12% / Top8 / 粘合→盈亏比 / 5%振幅 / 松锁利阶梯 / 佣金万1 / T+1")
    print("=" * 78)

    # 全期 · 滑点敏感
    print("\n【全期 · 滑点敏感】")
    print(f"  {'滑点':<8}{'收益%':>8}{'回撤%':>8}{'卡玛':>7}{'Alpha%':>9}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
    base = None
    for slip, lab in [(0.0, "0"), (0.001, "0.1%"), (0.002, "0.2%")]:
        m = run(ctx, names, FULL[0], FULL[1], slip)
        if slip == 0.001:
            base = m
        print(f"  {lab:<8}{m['ret']:>8.1f}{m['mdd']:>8.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>9.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}")

    # 详情（取 0.1% 现实滑点）
    print(f"\n【全期详情 · 滑点0.1%（现实口径）】")
    print(f"  期末资产估算: {CAP*(1+base['ret']/100):,.0f} 元（本金 {CAP:,.0f}）")
    print(f"  总收益 {base['ret']:+.1f}% | 沪深300同期 {base['bench']:+.1f}% | 超额Alpha {base['alpha']:+.1f}%")
    print(f"  最大回撤 {base['mdd']:.1f}% | 卡玛(收益/回撤) {base['calmar']}")
    print(f"  交易 {base['trades']}笔 | 胜率 {base['win']}% | 盈亏比 {base['pnl_ratio']}")
    print(f"  期末未平仓 {base['open_pos']}只 | 平均持有 {_avg_hold(base['_trades'], None) if False else '—'}")
    print(f"  出场分布: {base['exits']}")

    # 分季窗口（滑点0.1%）
    print(f"\n【分季窗口 · 滑点0.1% · 看跨期稳定性】")
    print(f"  {'窗口':<8}{'收益%':>8}{'回撤%':>8}{'卡玛':>7}{'Alpha%':>9}{'交易':>6}{'胜率%':>7}")
    for lab, s, e in WINDOWS:
        m = run(ctx, names, s, e, 0.001)
        print(f"  {lab:<8}{m['ret']:>8.1f}{m['mdd']:>8.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>9.1f}{m['trades']:>6}{m['win']:>7.1f}")

    print("\n⚠️ 口径：信号取自当日收盘、次日盘中撮合(已含滞后)；量比按5分钟重建；"
          "无手动优先级；进出场为真实5分钟级。")


if __name__ == "__main__":
    main()
