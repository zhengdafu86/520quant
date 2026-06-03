"""
参数寻优 sweep（在忠实回测引擎上 OFAT 单因子扫描）
================================================
单因子逐个扫（其余固定为基线），避免一次性多维网格过拟合。
目标：压低 25% 的最大回撤、并把真实执行下的 Alpha 拉离噪音区。

扫描的 4 个对症参数：
  - max_pos          最大持仓数（暴露）        基线 6
  - HARD_STOP_PCT    硬止损阈值                基线 -5
  - TREND_STOP_BARS  趋势止损确认根数           基线 2
  - DD_THRESH_MULT   浮盈回落容忍倍数           基线 1.0

用法:
  python -m backtest.sweep                       # 默认 150 只样本、全窗口
  python -m backtest.sweep --sample 150 --seed 42
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import monitor.intraday as IT
from backtest.intraday_portfolio import load_ctx, simulate, metrics
from data import intraday_store as ids

CAPITAL = 200_000

# 基线（=实盘对齐：精选Top8 + 盈亏比优先 + 现行止损止盈，三条止盈全开，无时间止损）
BASE = {"max_pos": 6, "hard_stop": -5.0, "trend_bars": 2, "dd_mult": 1.0,
        "top_n": 8, "no_peak_lock": False, "max_hold": 0}

# 单因子扫描网格
GRID = {
    "top_n":        [5, 8, 12, 0],      # 0=不限（全信号）
    "max_pos":      [3, 4, 5, 6],
    "hard_stop":    [-5.0, -6.0, -7.0, -8.0],
    "trend_bars":   [2, 3, 4],
    "dd_mult":      [1.0, 1.5, 2.0],
    "no_peak_lock": [False, True],      # True=三合二（关峰值锁利）
    "max_hold":     [0, 15, 20, 30],    # 条件时间止损上限（0=关）
}


def run_once(ctx, names, start, end, p) -> dict:
    IT.HARD_STOP_PCT     = p["hard_stop"]
    IT.TREND_STOP_BARS   = p["trend_bars"]
    IT.DD_THRESH_MULT    = p["dd_mult"]
    IT.DISABLE_PEAK_LOCK = p["no_peak_lock"]
    IT.MAX_HOLD_DAYS     = p["max_hold"]
    trades, ec, pos = simulate(ctx, names, start, end,
                               max_pos=p["max_pos"], capital=CAPITAL,
                               top_n=p["top_n"])
    return metrics(trades, ec, pos, CAPITAL, ctx["mk"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-05-22")
    ap.add_argument("--end", default="2026-06-02")
    a = ap.parse_args()

    # 直接用已回补进 intraday.db 的全部代码（稳定宇宙，用满数据）
    codes = ids.all_codes("5m")
    names = {c: c for c in codes}
    print(f"寻优 sweep：库内 {len(codes)} 只 | {a.start}~{a.end}")
    ctx = load_ctx(codes)
    print(f"  有效数据: {len(ctx['daily'])} 只\n")

    rows = []  # (标签, 参数, 指标)

    # 基线
    m = run_once(ctx, names, a.start, a.end, BASE)
    rows.append(("基线", dict(BASE), m))
    print(f"基线完成: 收益{m['ret']:+.1f}% 回撤{m['mdd']:.1f}% Alpha{m['alpha']:+.1f}%")

    # 逐因子扫描（其余固定基线）
    for factor, values in GRID.items():
        for v in values:
            if v == BASE[factor]:
                continue   # 基线值已跑
            p = dict(BASE); p[factor] = v
            m = run_once(ctx, names, a.start, a.end, p)
            rows.append((f"{factor}={v}", p, m))
            print(f"  {factor}={v}: 收益{m['ret']:+.1f}% 回撤{m['mdd']:.1f}% "
                  f"卡玛{m['calmar']} Alpha{m['alpha']:+.1f}% 交易{m['trades']}笔 胜率{m['win']}%")

    # ── 汇总表（按卡玛比率=收益/回撤 降序）──
    print("\n" + "=" * 84)
    print("  寻优结果（按 卡玛比率=收益/回撤 降序；基线见标记）")
    print("=" * 84)
    print(f"  {'参数变动':<16}{'收益%':>8}{'回撤%':>8}{'卡玛':>7}{'Alpha%':>8}{'交易':>6}{'胜率%':>7}{'盈亏比':>7}")
    rows_sorted = sorted(rows, key=lambda r: r[2].get("calmar", -99), reverse=True)
    for label, p, m in rows_sorted:
        tag = " ←基线" if label == "基线" else ""
        print(f"  {label:<16}{m['ret']:>8.1f}{m['mdd']:>8.1f}{m['calmar']:>7.2f}"
              f"{m['alpha']:>8.1f}{m['trades']:>6}{m['win']:>7.1f}{m['pnl_ratio']:>7.2f}{tag}")
    print("\n⚠️ 单区间单样本寻优易过拟合；赢家参数须在熊市段+另一样本复核后才可用。")


if __name__ == "__main__":
    main()
