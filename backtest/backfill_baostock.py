"""
BaoStock 分钟历史回补
================================================
用 BaoStock 把 A 股 5 分钟历史 K 线灌入 ~/.520quant/intraday.db（复用 intraday_store schema），
让 Phase 2 完整组合级忠实回测无需等待积累、立刻可跑。

复权口径：不复权（adjustflag=3），与 mootdx 日线、腾讯实时 5 分钟保持一致，
         避免 5分钟价 与 日线MA20 因复权基准不同而错位。

用法:
  python -m backtest.backfill_baostock --codes 600036,000001 --start 2025-01-01
  python -m backtest.backfill_baostock --sample 300 --start 2025-06-01 --end 2026-06-02
  python -m backtest.backfill_baostock --sample 0 --start 2025-06-01   # 全主板可交易池
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import baostock as bs

from data import intraday_store as ids


def _bs_code(code: str) -> str:
    return ("sh." if code.startswith(("6", "9", "5")) else "sz.") + code


def _fetch_5m(code: str, start: str, end: str) -> pd.DataFrame:
    """取单只 5 分钟历史，返回 [datetime, open, high, low, close, vol]"""
    rs = bs.query_history_k_data_plus(
        _bs_code(code), "date,time,open,high,low,close,volume",
        start_date=start, end_date=end, frequency="5", adjustflag="3")
    if rs.error_code != "0":
        return pd.DataFrame()
    data = []
    while rs.error_code == "0" and rs.next():
        data.append(rs.get_row_data())   # [date,time,open,high,low,close,volume]
    if not data:
        return pd.DataFrame()
    raw = pd.DataFrame(data, columns=["date", "time", "open", "high", "low", "close", "volume"])
    t = raw["time"].astype(str)
    out = pd.DataFrame({
        "datetime": (t.str[0:4] + "-" + t.str[4:6] + "-" + t.str[6:8]
                     + " " + t.str[8:10] + ":" + t.str[10:12] + ":00"),
        "open":  pd.to_numeric(raw["open"],  errors="coerce"),
        "high":  pd.to_numeric(raw["high"],  errors="coerce"),
        "low":   pd.to_numeric(raw["low"],   errors="coerce"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
        "vol":   pd.to_numeric(raw["volume"], errors="coerce").fillna(0.0),
    }).dropna(subset=["open", "high", "low", "close"])
    return out[["datetime", "open", "high", "low", "close", "vol"]]


def build_codes(args) -> list[str]:
    if args.codes:
        return [c.strip() for c in args.codes.split(",") if c.strip()]
    # 复用 replay 的宇宙构建（主板预过滤 + 抽样）
    from backtest.replay import build_universe
    return [s["code"] for s in build_universe(args.sample, args.seed)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes",  default="", help="逗号分隔代码；空则用 --sample 宇宙")
    ap.add_argument("--sample", type=int, default=300, help="抽样主板股数；0=全部")
    ap.add_argument("--seed",   type=int, default=42)
    ap.add_argument("--start",  default="2025-06-01", help="起始日期")
    ap.add_argument("--end",    default="", help="结束日期，默认今天")
    ap.add_argument("--skip-existing", type=int, default=200,
                    help="已入库交易日数≥此值则跳过（默认200，0=不跳过）")
    a = ap.parse_args()

    codes = build_codes(a)
    if a.skip_existing > 0:
        before = len(codes)
        codes = [c for c in codes if ids.code_days(c) < a.skip_existing]
        print(f"跳过已抓取 {before - len(codes)} 只，剩 {len(codes)} 只待回补")
    print(f"回补 {len(codes)} 只 5 分钟历史 | {a.start} ~ {a.end or '今天'}")

    lg = bs.login()
    if lg.error_code != "0":
        print(f"BaoStock 登录失败: {lg.error_msg}")
        return
    try:
        conn = ids._conn()
        ok = total = 0
        for i, code in enumerate(codes, 1):
            try:
                df = _fetch_5m(code, a.start, a.end)
                saved = ids.save_bars(code, "5m", df, conn)
                if saved:
                    ok += 1
                    total += saved
                if i % 50 == 0:
                    print(f"  进度 {i}/{len(codes)} | 已入库 {total} 根")
            except Exception as e:
                print(f"  {code} 失败: {e}")
        conn.close()
        print(f"\n回补完成: {ok}/{len(codes)} 只成功, 入库 {total} 根 5 分钟K")
        print("分钟库累计:", ids.stats())
    finally:
        bs.logout()


if __name__ == "__main__":
    main()
