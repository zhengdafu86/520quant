"""
追高保护机会成本诊断 — 被"偏离MA20<5%"拦掉的高分回踩候选，后续到底涨没涨？
================================================
口径(贴合线上): 看交易日T 买入窗口(10:00-14:30)的5分钟最低价 vs 信号日MA20。
  窗口内最低价仍 >MA20×(1+CHASE) ⇒ 全窗口没靠近MA20 ⇒ "追高拒绝"(线上不会买)。
对比 追高拒绝组 vs 可买组 的后续5/10日真实涨跌(T收盘为基准)。
逐只处理(每次只持有一只的5m)→ 内存安全。仅全年(2025-05-22~2026-06-02)回踩候选,去防御板块。
用法: python3 -B -m backtest.diagnose_chase
"""
from __future__ import annotations

import sys
import json
import statistics as st
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.intraday_portfolio import _precompute_candidates
from data.fetcher import db
from data import intraday_store as ids

S, E = "2025-05-22", "2026-06-02"
CHASE = 0.05
DEF = {"电力", "铁路公路", "燃气Ⅱ", "燃气", "公用事业", "港口", "水务",
       "高速公路", "供气供热", "航运港口", "电力Ⅱ"}
SM = json.load(open("/tmp/code_sector.json", encoding="utf-8"))


def _summ(rows, label):
    if not rows:
        print(f"  {label}: 0 个"); return
    f5 = [r[1] for r in rows if r[1] is not None]
    f10 = [r[2] for r in rows if r[2] is not None]
    pos5 = sum(1 for x in f5 if x > 0) / len(f5) * 100 if f5 else 0
    pos10 = sum(1 for x in f10 if x > 0) / len(f10) * 100 if f10 else 0
    print(f"  {label}: {len(rows)}个 | "
          f"后5日 均值{st.mean(f5)*100:+.2f}% 中位{st.median(f5)*100:+.2f}% 胜率{pos5:.0f}% | "
          f"后10日 均值{st.mean(f10)*100:+.2f}% 中位{st.median(f10)*100:+.2f}% 胜率{pos10:.0f}%")


def main():
    codes = ids.all_codes("5m")
    print(f"全市场 {len(codes)} 只 | 逐只诊断(5m买入窗口口径)…", flush=True)
    rejected, buyable, rej_hi, buy_hi = [], [], [], []
    n_pb = 0
    for c in codes:
        if SM.get(c, "") in DEF:
            continue
        d = db.get(c, freq="day", bars=320)
        if d is None or d.empty:
            continue
        d = d.copy(); d["d"] = d["datetime"].astype(str).str[:10]
        cm = _precompute_candidates(d)
        pb_days = [(T, asof, score) for T, (sig, asof, score) in cm.items()
                   if "回踩" in sig and S <= T <= E and asof < len(d)]
        if not pb_days:
            continue
        bars = ids.get_bars(c, "5m")     # 该只全部5m
        if not bars:
            continue
        m5 = {}
        for dt, o, h, l, cl, v in bars:
            m5.setdefault(dt[:10], []).append((dt[11:16], float(l), float(cl)))
        for T, asof, score in pb_days:
            ma20 = float(d.iloc[asof - 1]["ma20"])
            if ma20 <= 0:
                continue
            day = m5.get(T)
            if not day:
                continue
            win = [b for b in day if "10:00" <= b[0] <= "14:30"]   # 买入窗口
            if not win:
                continue
            wlow = min(b[1] for b in win)          # 窗口内最低价
            dev = (wlow - ma20) / ma20
            base = float(d.iloc[asof]["close"])
            fwd5 = (float(d.iloc[asof + 5]["close"]) / base - 1) if asof + 5 < len(d) and base > 0 else None
            fwd10 = (float(d.iloc[asof + 10]["close"]) / base - 1) if asof + 10 < len(d) and base > 0 else None
            n_pb += 1
            rec = (dev, fwd5, fwd10)
            tgt_all = rejected if dev > CHASE else buyable
            tgt_hi = (rej_hi if dev > CHASE else buy_hi)
            tgt_all.append(rec)
            if score >= 80:
                tgt_hi.append(rec)

    print(f"\n回踩候选(有5m且全年,去防御): {n_pb}")
    print(f"追高拒绝(买入窗口最低仍>MA20×1.05): {len(rejected)} ({len(rejected)/max(1,n_pb)*100:.1f}%) | "
          f"可买: {len(buyable)}")
    print("\n【全部回踩候选】后续真实涨跌(T收盘基准):")
    _summ(rejected, "追高拒绝组")
    _summ(buyable, "可买组    ")
    print("\n【高分回踩(评分≥80)】:")
    _summ(rej_hi, "追高拒绝组")
    _summ(buy_hi, "可买组    ")
    print("\n判读: 拒绝组后续涨≥可买组 ⇒ 漏赢家,放宽到6-7%; 涨更少/为负 ⇒ 拦得对,保持5%。")


if __name__ == "__main__":
    main()
