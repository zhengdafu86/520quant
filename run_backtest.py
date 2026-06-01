#!/usr/bin/env python3
"""
520战法 回测脚本
用法：
  # 用自选股（watchlist）
  python3 run_backtest.py --watchlist

  # 手动指定股票代码
  python3 run_backtest.py --codes 600519 000858 601318 300750 002415

  # 上次扫描结果
  python3 run_backtest.py --scan

  # 指定日期区间
  python3 run_backtest.py --codes 600519 000858 --start 2025-01-01 --end 2025-12-31

  # 调整资金 / 仓位数
  python3 run_backtest.py --codes 600519 000858 --capital 100000 --positions 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from data.fetcher import db
from backtest.engine import Backtester
from trader.paper import paper


# ── 工具函数 ──────────────────────────────────────────

def _fetch_names(codes: list[str]) -> dict[str, str]:
    """通过腾讯 API 批量拉取股票名称，返回 {code: name}"""
    import urllib.request, time
    result = {}
    BATCH = 80
    for i in range(0, len(codes), BATCH):
        batch = codes[i: i + BATCH]
        items = [("sh" if c.startswith(("6","9","5")) else "sz") + c for c in batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(items)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
            for line in raw.strip().split("\n"):
                if '="' not in line:
                    continue
                vals = line.split('="')[1].rstrip('";').split("~")
                if len(vals) >= 3 and vals[2]:
                    result[vals[2]] = vals[1]
        except Exception:
            pass
        time.sleep(0.08)
    return result


def _resolve_stocks(args) -> list[dict]:
    """根据参数决定股票池来源，返回 [{"code":..., "name":...}, ...]"""

    # ── 1. 手动指定 --codes ──────────────────────────
    if args.codes:
        codes  = [c.zfill(6) for c in args.codes]
        names  = _fetch_names(codes)
        return [{"code": c, "name": names.get(c, c)} for c in codes]

    # ── 2. 自选股 --watchlist ───────────────────────
    if args.watchlist:
        wl = paper.get_watchlist()
        if not wl:
            print("⚠️  自选股为空，请先添加股票或改用 --codes")
            sys.exit(1)
        return [{"code": w["code"], "name": w["name"]} for w in wl]

    # ── 3. 扫描结果 --scan ──────────────────────────
    if args.scan:
        rows = paper.get_scan_results()
        if not rows:
            print("⚠️  数据库中无扫描记录，请先运行一次扫描或改用 --codes")
            sys.exit(1)
        seen = set()
        stocks = []
        for r in rows:
            code = r[0] if isinstance(r, (list, tuple)) else r.get("code", "")
            name = r[1] if isinstance(r, (list, tuple)) else r.get("name", code)
            if code and code not in seen:
                seen.add(code)
                stocks.append({"code": code, "name": name})
        return stocks

    # ── 4. 默认：从 DB 里找有数据的股票 ─────────────
    print("未指定股票池，使用 --codes / --watchlist / --scan 之一")
    print("示例：python3 run_backtest.py --codes 600519 000858 601318")
    sys.exit(1)


def _get_priority_map(args, stocks: list[dict]) -> dict[str, str]:
    """如果来自自选股，读取优先级；否则返回空 dict"""
    if not args.watchlist:
        return {}
    wl = paper.get_watchlist()
    return {w["code"]: w.get("priority", "") for w in wl if w.get("priority")}


# ── 主程序 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="520战法回测")

    # 股票来源（三选一）
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--codes", nargs="+", metavar="CODE",
                     help="手动指定股票代码，如 --codes 600519 000858")
    src.add_argument("--watchlist", action="store_true",
                     help="使用 paper DB 自选股")
    src.add_argument("--scan", action="store_true",
                     help="使用 paper DB 最近扫描结果")

    # 时间区间
    parser.add_argument("--start", default="",
                        help="回测开始日期 YYYY-MM-DD（默认：近 250 个交易日）")
    parser.add_argument("--end", default="",
                        help="回测结束日期 YYYY-MM-DD（默认：最新数据）")
    parser.add_argument("--bars", type=int, default=250,
                        help="当未指定 --start 时，用最近 N 根日线（默认 250）")

    # 账户参数
    parser.add_argument("--capital", type=float, default=200_000,
                        help="初始资金（元，默认 200000）")
    parser.add_argument("--positions", type=int, default=4,
                        help="最大同时持仓数（默认 4）")

    args = parser.parse_args()

    # ── 检查至少指定了一种股票来源 ──
    if not args.codes and not args.watchlist and not args.scan:
        parser.print_help()
        print("\n错误：请指定股票来源（--codes / --watchlist / --scan）")
        sys.exit(1)

    stocks       = _resolve_stocks(args)
    priority_map = _get_priority_map(args, stocks)

    bt = Backtester(init_capital=args.capital, max_positions=args.positions)
    bt.run(
        stocks       = stocks,
        bars         = args.bars,
        start_date   = args.start,
        end_date     = args.end,
        priority_map = priority_map,
    )


if __name__ == "__main__":
    main()
