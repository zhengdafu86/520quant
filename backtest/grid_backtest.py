"""
沪深300ETF(510300) 网格策略原型回测
================================================
等比网格: 价位 = base×(1+step)^k。每跌到一格买一手、每涨到上一格卖一手(赚step价差)。
T+1约束; ETF费率万2(无印花)。起始按"装满到base价"的底仓。
对照买入持有。测全期 + 近段震荡。
用法: python3 -B -m backtest.grid_backtest
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.fetcher import db

CAP = 200_000
FEE = 0.0002   # ETF 佣金双边约万2, 无印花税


def run_grid(rows, step, n, base):
    """rows: [(date, low, high, close)]; step: 每格%; n: 上下各n格; base: 中枢价。"""
    levels = [round(base * (1 + step) ** k, 3) for k in range(-n, n + 1)]
    lot_val = CAP / (n + 2)             # 每格金额(留余量,跌到底也不爆)
    cash = CAP
    filled = {}                          # level_price -> (buy_date, shares)
    realized = 0.0
    # 底仓: 起始把 ≤base 的格子都买上(有货可卖)
    d0 = rows[0][0]; p0 = rows[0][3]
    for lv in levels:
        if lv <= p0:
            sh = int(lot_val / lv / 100) * 100
            if sh >= 100 and cash >= lv * sh:
                cash -= lv * sh * (1 + FEE); filled[lv] = (d0, sh)
    init_shares = sum(s for _, s in filled.values())
    rt = 0                               # 完成的网格round-trip数
    eq_curve = []
    for date, low, high, close in rows:
        # 买: 价格下探到空格
        for lv in levels:
            if lv not in filled and low <= lv:
                sh = int(lot_val / lv / 100) * 100
                if sh >= 100 and cash >= lv * sh * (1 + FEE):
                    cash -= lv * sh * (1 + FEE); filled[lv] = (date, sh)
        # 卖: 持仓格子涨到"上一格"卖出(T+1)
        for lv in list(filled):
            sell_at = round(lv * (1 + step), 3)
            bd, sh = filled[lv]
            if high >= sell_at and bd < date:
                proceeds = sell_at * sh * (1 - FEE)
                cash += proceeds
                realized += proceeds - lv * sh * (1 + FEE)
                del filled[lv]; rt += 1
        held = sum(s for _, s in filled.values())
        eq_curve.append(cash + held * close)
    final = eq_curve[-1]
    peak = CAP; mdd = 0.0
    for e in eq_curve:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak * 100)
    held_val = sum(s for _, s in filled.values()) * rows[-1][3]
    return {"ret": (final / CAP - 1) * 100, "mdd": mdd, "rt": rt, "realized": realized,
            "held_pct": held_val / final * 100, "init_sh": init_shares}


def main():
    mk = db.get_market(bars=400).copy()
    mk["d"] = mk["datetime"].astype(str).str[:10]
    def rows_of(s, e):
        seg = mk[(mk["d"] >= s) & (mk["d"] <= e)]
        return [(r["d"], float(r["low"]), float(r["high"]), float(r["close"]))
                for _, r in seg.iterrows()]

    WINS = [("全期", "2025-05-22", "2026-06-02"), ("近120日", "2026-01-02", "2026-06-02"),
            ("近60日(震荡)", "2026-03-10", "2026-06-02")]
    for lab, s, e in WINS:
        rows = rows_of(s, e)
        if not rows:
            continue
        bh = (rows[-1][3] / rows[0][3] - 1) * 100
        base = rows[0][3]
        print(f"\n── {lab} {s}~{e} | 买入持有 {bh:+.1f}%")
        print(f"   {'网格参数':<16}{'网格收益%':>9}{'回撤%':>7}{'成交对数':>8}{'期末持仓%':>9}")
        for step in (0.015, 0.02, 0.03):
            for n in (10, 15):
                m = run_grid(rows, step, n, base)
                print(f"   step{step*100:.1f}%·{n}格{'':<6}"[:16]
                      + f"{m['ret']:>9.1f}{m['mdd']:>7.1f}{m['rt']:>8}{m['held_pct']:>8.0f}%")
    print("\n判读: 网格收益 vs 买入持有——趋势市网格大概率跑输(卖飞);若某震荡段网格>买持,才说明它有用。")


if __name__ == "__main__":
    main()
