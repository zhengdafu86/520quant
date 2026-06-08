"""
每日热门板块/题材采集（同花顺热点 → 题材聚合）
================================================
数据源：同花顺热点 zx.10jqka.com.cn（当日强势股 + 人工题材归因标签）。
不走东财（push2 对云服务器封禁），服务器可达。

做法：拉当日强势股 → 每只 reason 按 '+' 拆成题材标签 → 统计各标签命中的强势股数
     → 按命中数降序得「今日最热题材榜」，存 market.db.hot_themes。

用法:
  python -m scanner.hot_sectors --collect     # 采集入库
  python -m scanner.hot_sectors --show        # 查看最新榜单
"""
from __future__ import annotations

import sys
import json
import time
import sqlite3
import argparse
from datetime import datetime
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from trader.paper import MARKET_DB

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/117 Safari/537.36"
_THS = ("http://zx.10jqka.com.cn/event/api/getharden/"
        "date/{date}/orderby/date/orderway/desc/charset/GBK/")
# 过滤无信息量/噪音标签（业绩/财务类，不是"题材板块"）
_STOP = {"一季报增长", "次新股", "业绩增长", "高送转", "举牌",
         "一季报扭亏", "一季度扭亏", "扭亏为盈", "回购进展",
         "年报增长", "中报增长", "半年报增长", "业绩预增", "股东增持"}


def _init(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hot_themes (
            date        TEXT,
            rank        INTEGER,
            theme       TEXT,
            stock_count INTEGER,
            stocks      TEXT,
            PRIMARY KEY (date, theme)
        )""")
    conn.commit()


def fetch_strong(date: str = None) -> list[dict]:
    """同花顺当日强势股 [{name, code, reason}]。失败返回 []。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    url = _THS.format(date=date)
    for i in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=10)
            j = r.json()
            if j.get("errocode", 0) == 0:
                return [{"name": x.get("name", ""), "code": x.get("code", ""),
                         "reason": x.get("reason", "")}
                        for x in (j.get("data") or [])]
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return []


def aggregate(strong: list[dict], top: int = 25) -> list[dict]:
    """题材标签聚合 → [{theme, stock_count, stocks:[名称...]}]，按命中数降序。"""
    theme_stocks = defaultdict(list)
    for s in strong:
        seen = set()
        for tag in (s.get("reason") or "").split("+"):
            tag = tag.strip()
            if not tag or tag in _STOP or tag in seen:
                continue
            seen.add(tag)
            theme_stocks[tag].append(s["name"])
    ranked = sorted(theme_stocks.items(), key=lambda kv: -len(kv[1]))
    out = []
    for theme, names in ranked:
        if len(names) < 2:        # 只出现1次的标签不算"热门板块"
            continue
        out.append({"theme": theme, "stock_count": len(names), "stocks": names})
    return out[:top]


def collect_to_db(date: str = None) -> int:
    """采集当日热门题材并入库，返回入库题材数。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    strong = fetch_strong(date)
    if not strong:
        return 0
    themes = aggregate(strong)
    conn = sqlite3.connect(str(MARKET_DB))
    _init(conn)
    conn.execute("DELETE FROM hot_themes WHERE date=?", (date,))
    for i, t in enumerate(themes, 1):
        conn.execute(
            "INSERT OR REPLACE INTO hot_themes VALUES (?,?,?,?,?)",
            (date, i, t["theme"], t["stock_count"], ",".join(t["stocks"][:12])))
    conn.commit()
    conn.close()
    return len(themes)


def get_hot(date: str = None, top: int = 20) -> dict:
    """读取最新（或指定日）热门题材榜。"""
    conn = sqlite3.connect(str(MARKET_DB))
    _init(conn)
    if not date:
        row = conn.execute("SELECT MAX(date) FROM hot_themes").fetchone()
        date = row[0] if row and row[0] else ""
    rows = conn.execute(
        "SELECT rank,theme,stock_count,stocks FROM hot_themes "
        "WHERE date=? ORDER BY rank LIMIT ?", (date, top)).fetchall()
    conn.close()
    return {"date": date, "themes": [
        {"rank": r[0], "theme": r[1], "stock_count": r[2],
         "stocks": (r[3] or "").split(",")} for r in rows]}


def stock_hot_themes(codes, date: str = None) -> dict:
    """给定 codes，返回 {code: [命中的热门题材标签]}。
    = 该股在同花顺强势股里的题材标签 ∩ 当日热门题材榜（命中≥2只的题材）。
    用于 AI 评分：踩中当日热点的个股给加分。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    strong = fetch_strong(date)
    code_tags = {
        s["code"]: [t.strip() for t in (s.get("reason") or "").split("+")
                    if t.strip() and t.strip() not in _STOP]
        for s in strong
    }
    hot = {t["theme"] for t in get_hot(date, top=50)["themes"]}
    return {c: [t for t in code_tags.get(c, []) if t in hot] for c in codes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    if a.collect:
        n = collect_to_db(a.date)
        print(f"✅ 热门题材已入库 {n} 个")
    elif a.show:
        d = get_hot(a.date)
        print(f"日期 {d['date']} | 热门题材 {len(d['themes'])} 个")
        for t in d["themes"]:
            print(f"  #{t['rank']:<2} {t['theme']:<16} {t['stock_count']:>2}只  "
                  f"{'/'.join(t['stocks'][:5])}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
