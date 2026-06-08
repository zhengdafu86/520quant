"""
【在回测机运行】把 xtdata 导出的 sqlite 导入 intraday.db
================================================
将 xtdata_export.py 产出的 xt_*.db 合并进 ~/.520quant/intraday.db 的 minute_bars，
供现有回测引擎直接读取（ids.all_codes / get_bars）。

用法:
  python -m data.xtdata_import xt_5m.db [--replace-freq 5m]
--replace-freq：导入前先清空该 freq 的旧数据（用 xtdata 全量替换，避免新旧源混用）。
"""
from __future__ import annotations

import sys
import sqlite3
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DEST = Path.home() / ".520quant" / "intraday.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="xtdata 导出的 sqlite 文件")
    ap.add_argument("--replace-freq", default="", help="导入前清空该freq旧数据(如 5m)")
    a = ap.parse_args()

    if not Path(a.src).exists():
        print(f"❌ 源文件不存在: {a.src}"); return
    src = sqlite3.connect(a.src)
    rows = src.execute("SELECT code,freq,datetime,open,high,low,close,vol FROM minute_bars").fetchall()
    src.close()
    print(f"源 {a.src}: {len(rows)} 根")

    DEST.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(DEST))
    dst.execute("""CREATE TABLE IF NOT EXISTS minute_bars(
        code TEXT, freq TEXT, datetime TEXT, open REAL, high REAL,
        low REAL, close REAL, vol REAL, PRIMARY KEY(code,freq,datetime))""")
    if a.replace_freq:
        n = dst.execute("DELETE FROM minute_bars WHERE freq=?", (a.replace_freq,)).rowcount
        print(f"已清空旧 {a.replace_freq} 数据 {n} 根")
    dst.executemany("INSERT OR REPLACE INTO minute_bars VALUES (?,?,?,?,?,?,?,?)", rows)
    dst.commit()
    mn, mx = dst.execute("SELECT MIN(datetime),MAX(datetime) FROM minute_bars").fetchone()
    nc = dst.execute("SELECT COUNT(DISTINCT code) FROM minute_bars").fetchone()[0]
    dst.close()
    print(f"✅ 已导入 {DEST} | {nc}只 | {mn}~{mx}")
    print("接着可直接跑回测：python3 -B -m backtest.live_report（会用新数据+ctx缓存）")


if __name__ == "__main__":
    main()
