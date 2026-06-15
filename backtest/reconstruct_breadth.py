"""
重构历史涨跌比序列 — 从全宇宙历史日线统计每日涨/跌家数(供宽度类信号回测)。
口径: 宇宙(1055主板) 每日 收盘>昨收=涨家 / <昨收=跌家。存 ~/.520quant/breadth_hist.json。
用法: python3 -B -m backtest.reconstruct_breadth
"""
from __future__ import annotations

import sys
import os
import json
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.fetcher import db
from data import intraday_store as ids

OUT = os.path.expanduser("~/.520quant/breadth_hist.json")


def main():
    codes = ids.all_codes("5m")
    print(f"宇宙 {len(codes)} 只 | 拉历史日线重构涨跌比…", flush=True)
    up = defaultdict(int); down = defaultdict(int)
    n = 0
    for i, c in enumerate(codes):
        d = db.get(c, freq="day", bars=400)
        if d is None or d.empty:
            continue
        cl = d["close"].astype(float).values
        dts = d["datetime"].astype(str).str[:10].values
        for k in range(1, len(cl)):
            if cl[k] > cl[k - 1]:
                up[dts[k]] += 1
            elif cl[k] < cl[k - 1]:
                down[dts[k]] += 1
        n += 1
        if (i + 1) % 300 == 0:
            print(f"  {i+1}/{len(codes)}", flush=True)

    dates = sorted(set(up) | set(down))
    series = {dt: [up.get(dt, 0), down.get(dt, 0)] for dt in dates}
    json.dump(series, open(OUT, "w"))
    print(f"\n重构完成: {n}只 | {len(dates)}个交易日 | 存 {OUT}")

    # 分季均值 + 与大盘对照
    mk = db.get_market(bars=400).copy(); mk["d"] = mk["datetime"].astype(str).str[:10]
    Q = [("Q1夏", "2025-05-22", "2025-08-31"), ("Q2秋", "2025-09-01", "2025-11-30"),
         ("Q3冬", "2025-12-01", "2026-02-28"), ("Q4春", "2026-03-01", "2026-06-02")]
    print(f"\n{'季度':<6}{'平均涨跌比':>10}{'普涨天占比':>11}{'普跌天占比':>11}{'沪深300%':>9}")
    for lab, s, e in Q:
        ds = [dt for dt in dates if s <= dt <= e]
        if not ds:
            continue
        ratios = [up[dt] / down[dt] if down[dt] else 9.99 for dt in ds]
        strong = sum(1 for r in ratios if r >= 1.5) / len(ratios) * 100
        weak = sum(1 for r in ratios if r <= 0.67) / len(ratios) * 100
        avg = sum(ratios) / len(ratios)
        seg = mk[(mk["d"] >= s) & (mk["d"] <= e)]
        bench = (float(seg.iloc[-1]["close"]) / float(seg.iloc[0]["close"]) - 1) * 100 if len(seg) else 0
        print(f"{lab:<6}{avg:>10.2f}{strong:>10.0f}%{weak:>10.0f}%{bench:>9.1f}")
    print("\n看: 平均涨跌比/普涨占比 是否和大盘季度涨幅同向(验证宽度有信息) → 下一步可做宽度过滤回测。")


if __name__ == "__main__":
    main()
