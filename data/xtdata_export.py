"""
【在 Windows + QMT 上运行】用 xtdata 下载历史K线 → 导出便携 sqlite
================================================
把回测宇宙的分钟K从 QMT 本地行情下载并导出，供回测机导入(intraday.db)。
默认 5m（与线上策略口径一致）。策略零改动，仅升级数据来源。

用法（Windows QMT 端）:
  python xtdata_export.py --freq 5m --start 20250522 --end 20260605 \
         --codes codes.txt --out xt_5m.db
codes.txt：每行一个6位代码；缺省则取沪深主板A股(排除创业板/科创板)。
导出后把 xt_5m.db rsync/scp 到回测机，再跑 xtdata_import.py。
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def _main_board_codes():
    """xtdata 取沪深A股 → 仅主板(排除300/301/688/689/8/4北交所)。"""
    from xtquant import xtdata
    out = []
    for sec in xtdata.get_stock_list_in_sector("沪深A股"):
        code = sec.split(".")[0]
        if code[:3] in ("300", "301", "688", "689") or code[0] in ("8", "4"):
            continue
        out.append(code)
    return out


def _secid(code):
    return (code + ".SH") if code.startswith(("6", "9", "5")) else (code + ".SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="5m", choices=["1m", "5m", "15m", "1d"])
    ap.add_argument("--start", default="20250522")
    ap.add_argument("--end", default="")
    ap.add_argument("--codes", default="")
    ap.add_argument("--out", default="xt_export.db")
    a = ap.parse_args()

    from xtquant import xtdata
    period = {"1m": "1m", "5m": "5m", "15m": "15m", "1d": "1d"}[a.freq]

    if a.codes and Path(a.codes).exists():
        codes = [x.strip() for x in Path(a.codes).read_text().splitlines() if x.strip()]
    else:
        codes = _main_board_codes()
    print(f"导出 {len(codes)} 只 | {a.freq} | {a.start}~{a.end or '今'}")

    conn = sqlite3.connect(a.out)
    conn.execute("""CREATE TABLE IF NOT EXISTS minute_bars(
        code TEXT, freq TEXT, datetime TEXT, open REAL, high REAL,
        low REAL, close REAL, vol REAL, PRIMARY KEY(code,freq,datetime))""")
    fld = ["time", "open", "high", "low", "close", "volume"]
    ok = 0
    for n, code in enumerate(codes, 1):
        sec = _secid(code)
        try:
            xtdata.download_history_data(sec, period, a.start, a.end)
            d = xtdata.get_market_data_ex(fld, [sec], period=period,
                                          start_time=a.start, end_time=a.end)
            df = d.get(sec)
            if df is None or len(df) == 0:
                continue
            rows = []
            times = df["time"] if "time" in df else df.index
            for i in range(len(df)):
                t = str(times[i] if "time" in df else df.index[i])
                # xtdata 时间 '20260605093500' → 'YYYY-MM-DD HH:MM:SS'
                if len(t) >= 14 and t.isdigit():
                    dt = f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"
                else:
                    dt = t
                rows.append((code, a.freq, dt, float(df["open"].iloc[i]),
                             float(df["high"].iloc[i]), float(df["low"].iloc[i]),
                             float(df["close"].iloc[i]), float(df["volume"].iloc[i])))
            conn.executemany("INSERT OR REPLACE INTO minute_bars VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.commit(); ok += 1
        except Exception as e:
            print(f"  {code} 失败: {e}")
        if n % 100 == 0:
            print(f"  …{n}/{len(codes)}")
    conn.close()
    print(f"✅ 导出完成 {ok}/{len(codes)} 只 → {a.out}")


if __name__ == "__main__":
    main()
