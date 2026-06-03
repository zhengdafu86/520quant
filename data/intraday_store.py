"""
盘中分钟数据落库
================================================
目的：为"忠实回测 check_entry/check_position 盘中逻辑"积累历史 5 分钟 K 线。
当前回测用日线 开盘/收盘 撮合，与实盘"盘中确认后才买"差异大；要消除该偏差，
必须有分钟级历史回放。mootdx 5分钟K只能取近期、无法补历史，故本模块
【从今天起每个交易日收盘后采集、向前积累】。

数据性质：市场数据，全用户共享（独立库 ~/.520quant/intraday.db）。
采集范围：所有用户的 持仓 + 自选 + 当日扫描结果（策略真正会操作的票）。

用法:
  python -m data.intraday_store            # 采集一次（默认宇宙）
  python -m data.intraday_store --stats    # 查看累计入库情况
  python -m data.intraday_store --codes 600036,000001   # 指定代码采集
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from data.fetcher import fetch_minute

INTRADAY_DB = Path.home() / ".520quant" / "intraday.db"


def _conn() -> sqlite3.Connection:
    INTRADAY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INTRADAY_DB), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS minute_bars (
            code     TEXT,
            freq     TEXT,
            datetime TEXT,
            open     REAL,
            high     REAL,
            low      REAL,
            close    REAL,
            vol      REAL,
            PRIMARY KEY (code, freq, datetime)
        )
    """)
    conn.commit()
    return conn


def save_bars(code: str, freq: str, df, conn: sqlite3.Connection = None) -> int:
    """落库单只股票的分钟K（INSERT OR IGNORE 去重，重复采集安全）"""
    if df is None or getattr(df, "empty", True):
        return 0
    own = conn is None
    conn = conn or _conn()
    cols = set(df.columns)
    m = len(df)

    def _col(c):
        return df[c].tolist() if c in cols else [0.0] * m

    # 向量化取列（避免 iterrows，1万+行快很多）
    dts    = df["datetime"].astype(str).tolist()
    opens, highs, lows = _col("open"), _col("high"), _col("low")
    closes, vols = _col("close"), _col("vol")
    rows = [(code, freq, dts[i], opens[i], highs[i], lows[i], closes[i], vols[i])
            for i in range(m)]
    n = 0
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO minute_bars VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        n = len(rows)
    if own:
        conn.close()
    return n


def collect(codes: list[str], freq: str = "5m", count: int = 320) -> tuple[int, int, int]:
    """采集一批股票的分钟K入库。返回 (成功股数, 入库K线数, 请求股数)"""
    uniq = sorted({c for c in codes if c})
    conn = _conn()
    ok = total = 0
    for code in uniq:
        try:
            dfx = fetch_minute(code, freq=freq, count=count)
            saved = save_bars(code, freq, dfx, conn)
            if saved:
                ok += 1
                total += saved
        except Exception:
            pass
    conn.close()
    return ok, total, len(uniq)


def collect_default(freq: str = "5m") -> tuple[int, int, int]:
    """采集默认宇宙：所有用户 持仓 + 自选 + 当日扫描结果（策略会操作的票）"""
    codes: set[str] = set()
    try:
        from auth.users import list_users
        from trader.paper import PaperAccount
        users = [u["username"] for u in list_users()]
        for i, un in enumerate(users):
            pu = PaperAccount(user=un)
            if i == 0:   # 扫描结果共享，取一次即可
                for r in pu.get_scan_results().get("results", []):
                    codes.add(r["code"])
            codes |= set(pu.positions().keys())
            codes |= {w["code"] for w in pu.get_watchlist()}
    except Exception:
        pass
    if not codes:
        return 0, 0, 0
    return collect(sorted(codes), freq=freq)


def get_bars(code: str, freq: str = "5m", date: str = None) -> list[tuple]:
    """读取某股票分钟K（回测回放用）。date=YYYY-MM-DD 取当日，否则全部。"""
    conn = _conn()
    try:
        if date:
            rows = conn.execute(
                "SELECT datetime,open,high,low,close,vol FROM minute_bars "
                "WHERE code=? AND freq=? AND substr(datetime,1,10)=? ORDER BY datetime",
                (code, freq, date)).fetchall()
        else:
            rows = conn.execute(
                "SELECT datetime,open,high,low,close,vol FROM minute_bars "
                "WHERE code=? AND freq=? ORDER BY datetime", (code, freq)).fetchall()
        return rows
    finally:
        conn.close()


def all_codes(freq: str = "5m") -> list[str]:
    """库内已有分钟数据的全部股票代码（回测/寻优用稳定宇宙）"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT code FROM minute_bars WHERE freq=? ORDER BY code",
            (freq,)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def code_days(code: str, freq: str = "5m") -> int:
    """某股票已入库的交易日数（用于回补时跳过已抓取的）"""
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT COUNT(DISTINCT substr(datetime,1,10)) FROM minute_bars "
            "WHERE code=? AND freq=?", (code, freq)).fetchone()
        return int(r[0]) if r else 0
    finally:
        conn.close()


def stats() -> dict:
    """累计入库情况"""
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT COUNT(DISTINCT code), COUNT(DISTINCT substr(datetime,1,10)), COUNT(*) "
            "FROM minute_bars WHERE freq='5m'").fetchone()
        first = conn.execute(
            "SELECT MIN(substr(datetime,1,10)), MAX(substr(datetime,1,10)) "
            "FROM minute_bars WHERE freq='5m'").fetchone()
        return {"codes": r[0], "days": r[1], "bars": r[2],
                "from": first[0], "to": first[1]}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--stats" in args:
        print("分钟数据累计:", stats())
    elif "--codes" in args:
        idx = args.index("--codes")
        codes = args[idx + 1].split(",") if idx + 1 < len(args) else []
        ok, total, n = collect(codes)
        print(f"采集完成: {ok}/{n} 只成功, 入库 {total} 根5分钟K")
        print("累计:", stats())
    else:
        ok, total, n = collect_default()
        print(f"采集完成: {ok}/{n} 只成功, 入库 {total} 根5分钟K")
        print("累计:", stats())
