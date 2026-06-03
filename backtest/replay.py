"""
历史信号重建回测（无偏版）
================================================
对【宽基主板股票池】做 point-in-time 信号回放，消除"用今天扫描名单倒推"
带来的选股偏差——回测期内每一天的买点，都只用截至当天的数据判定（无未来函数）。

与直接回测"今天扫描结果"的区别：
  - 直接回测今天的 N 只：这些票是"活到今天且当前处于上升趋势"的幸存者 → 严重高估
  - 本脚本：从全主板随机/全量抽样，不按当前趋势挑票 → 接近真实可交易宇宙

用法:
  python -m backtest.replay                         # 默认随机抽样 300 只主板，近250交易日
  python -m backtest.replay --sample 0              # 全主板（很慢，建议后台跑）
  python -m backtest.replay --sample 500 --bars 250
  python -m backtest.replay --start 2025-06-01 --end 2026-06-02 --max-pos 6

残留偏差（已无法完全消除，需知悉）:
  当前主板宇宙不含回测期内【已退市】的股票（退市幸存者偏差）。
  但"只选今天上升趋势票"这一主要偏差已被消除。
"""
from __future__ import annotations

import sys
import random
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.market_scan import scanner, _is_excluded_board
from backtest.engine import Backtester


def build_universe(sample: int, seed: int) -> list[dict]:
    """构建可交易主板宇宙：全市场 → 排除创业/科创 → 流动性/ST/市值预过滤 → 抽样"""
    codes = scanner._get_all_codes()
    codes = [c for c in codes if not _is_excluded_board(c)]
    print(f"主板代码: {len(codes)} 只（已排除创业板/科创板）")

    # 预过滤（与扫描器同口径：价格/成交额/ST/PE/市值），同时拿到股票名称
    passed = scanner._pre_filter(codes)          # [(code, name, price), ...]
    stocks = [{"code": c, "name": n} for c, n, _ in passed]
    print(f"预过滤后可交易: {len(stocks)} 只")

    if sample and 0 < sample < len(stocks):
        random.seed(seed)
        stocks = random.sample(stocks, sample)
        print(f"随机抽样: {len(stocks)} 只 (seed={seed}, 可复现)")
    return stocks


def main():
    ap = argparse.ArgumentParser(description="历史信号重建回测（无偏）")
    ap.add_argument("--sample",  type=int,   default=300, help="抽样股票数；0=全主板")
    ap.add_argument("--bars",    type=int,   default=250, help="回测交易日数（start 优先）")
    ap.add_argument("--start",   default="",  help="开始日期 2025-06-01")
    ap.add_argument("--end",     default="",  help="结束日期，默认最新")
    ap.add_argument("--max-pos", type=int,   default=6,   help="最大持仓数")
    ap.add_argument("--capital", type=float, default=200_000, help="初始资金")
    ap.add_argument("--seed",    type=int,   default=42,  help="抽样随机种子（可复现）")
    ap.add_argument("--slippage", type=float, default=0.0, help="每边滑点，如 0.001=0.1%")
    a = ap.parse_args()

    print("=" * 65)
    print("  历史信号重建回测（无偏）")
    print(f"  抽样: {a.sample or '全主板'}  |  区间: "
          f"{a.start or f'近{a.bars}日'} ~ {a.end or '最新'}  |  seed={a.seed}")
    print("=" * 65)

    stocks = build_universe(a.sample, a.seed)
    if not stocks:
        print("无可用股票，终止")
        return

    bt = Backtester(init_capital=a.capital, max_positions=a.max_pos,
                    slippage=a.slippage)
    bt.run(stocks, bars=a.bars, start_date=a.start, end_date=a.end)

    print("\n⚠️ 残留偏差提醒：当前宇宙不含回测期内已退市股票（退市幸存者偏差）；")
    print("   但已消除'只选今天上升趋势票'的主要偏差，结果比直接回测今日扫描名单可信得多。")


if __name__ == "__main__":
    main()
