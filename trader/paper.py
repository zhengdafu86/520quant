"""
模拟交易账户
- 真实行情 + 虚拟资金
- SQLite 持久化（重启不丢数据）
- 自动执行买卖信号
- 实时盈亏 + 绩效统计
"""
from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


DB_PATH = Path.home() / ".520quant" / "paper_trade.db"


# ── 数据结构 ──────────────────────────────────────────────

@dataclass
class Order:
    id:         int
    code:       str
    name:       str
    side:       str          # BUY / SELL
    price:      float
    shares:     int
    amount:     float        # 成交金额
    signal:     str          # 触发原因
    timestamp:  str


@dataclass
class PaperPosition:
    code:       str
    name:       str
    cost:       float        # 均价
    shares:     int
    stop_price: float
    entry_time: str

    def market_value(self, price: float) -> float:
        return price * self.shares

    def pnl(self, price: float) -> float:
        return (price - self.cost) * self.shares

    def pnl_pct(self, price: float) -> float:
        return (price - self.cost) / self.cost * 100


# ── 数据库初始化 ──────────────────────────────────────────

def _init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS account (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            code       TEXT,
            name       TEXT,
            side       TEXT,
            price      REAL,
            shares     INTEGER,
            amount     REAL,
            signal     TEXT,
            timestamp  TEXT
        );

        CREATE TABLE IF NOT EXISTS positions (
            code        TEXT PRIMARY KEY,
            name        TEXT,
            cost        REAL,
            shares      INTEGER,
            stop_price  REAL,
            entry_time  TEXT
        );

        CREATE TABLE IF NOT EXISTS watchlist (
            code       TEXT PRIMARY KEY,
            name       TEXT,
            signal     TEXT,
            added_time TEXT
        );

        CREATE TABLE IF NOT EXISTS scan_results (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date  TEXT,
            code       TEXT,
            name       TEXT,
            price      REAL,
            signal     TEXT,
            reason     TEXT,
            score      REAL,
            stop_price REAL
        );
    """)
    conn.commit()

    # 初始化账户资金（首次）
    cur = conn.execute("SELECT value FROM account WHERE key='cash'")
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO account VALUES ('cash', ?)",
            (str(INIT_CAPITAL),)
        )
        conn.execute(
            "INSERT INTO account VALUES ('init_capital', ?)",
            (str(INIT_CAPITAL),)
        )
        conn.commit()


# ── 模拟账户 ──────────────────────────────────────────────

INIT_CAPITAL = 100_000.0    # 初始资金（可在启动时修改）


class PaperAccount:
    """
    模拟交易账户
    所有操作通过 SQLite 持久化，重启后恢复状态
    """

    def __init__(self, init_capital: float = INIT_CAPITAL):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _init_db(self._conn)

        # 首次运行设置初始资金
        cur = self._conn.execute("SELECT value FROM account WHERE key='cash'")
        row = cur.fetchone()
        if row and float(row[0]) == INIT_CAPITAL and init_capital != INIT_CAPITAL:
            self._set("cash", init_capital)
            self._set("init_capital", init_capital)

    # ── 账户基础读写 ──────────────────────────────────

    def _get(self, key: str, default=0.0) -> float:
        cur = self._conn.execute(
            "SELECT value FROM account WHERE key=?", (key,)
        )
        row = cur.fetchone()
        return float(row[0]) if row else default

    def _set(self, key: str, value: float):
        self._conn.execute(
            "INSERT OR REPLACE INTO account VALUES (?,?)",
            (key, str(value))
        )
        self._conn.commit()

    @property
    def cash(self) -> float:
        return self._get("cash")

    @property
    def init_capital(self) -> float:
        return self._get("init_capital", INIT_CAPITAL)

    # ── 持仓管理 ──────────────────────────────────────

    def positions(self) -> dict[str, PaperPosition]:
        rows = self._conn.execute("SELECT * FROM positions").fetchall()
        result = {}
        for r in rows:
            result[r[0]] = PaperPosition(
                code=r[0], name=r[1], cost=r[2],
                shares=r[3], stop_price=r[4], entry_time=r[5]
            )
        return result

    def get_position(self, code: str) -> Optional[PaperPosition]:
        cur = self._conn.execute(
            "SELECT * FROM positions WHERE code=?", (code,)
        )
        r = cur.fetchone()
        if not r:
            return None
        return PaperPosition(
            code=r[0], name=r[1], cost=r[2],
            shares=r[3], stop_price=r[4], entry_time=r[5]
        )

    def update_stop(self, code: str, new_stop: float) -> bool:
        """更新持仓止损价（追踪止损专用，只升不降）"""
        pos = self.get_position(code)
        if not pos:
            return False
        if new_stop <= pos.stop_price:
            return False   # 止损线只升不降
        self._conn.execute(
            "UPDATE positions SET stop_price=? WHERE code=?",
            (round(new_stop, 2), code)
        )
        self._conn.commit()
        return True

    # ── 交易执行 ──────────────────────────────────────

    def buy(self, code: str, name: str, price: float, shares: int,
            signal: str = "", stop_price: float = 0.0) -> tuple[bool, str]:
        """
        模拟买入
        返回 (成功, 消息)
        """
        shares = int(shares // 100 * 100)   # 取整到100股
        if shares <= 0:
            return False, "买入数量不足100股"

        amount = round(price * shares, 2)
        commission = round(amount * 0.0003, 2)   # 万3佣金
        total_cost = amount + commission

        if total_cost > self.cash:
            max_shares = int(self.cash / price / 100) * 100
            if max_shares <= 0:
                return False, f"现金不足（剩余 {self.cash:.0f} 元）"
            shares = max_shares
            amount = round(price * shares, 2)
            commission = round(amount * 0.0003, 2)
            total_cost = amount + commission

        # 检查是否已有持仓（不加仓）
        existing = self.get_position(code)
        if existing:
            return False, f"已有持仓，520战法不加仓（现有 {existing.shares} 股）"

        # 写入订单
        self._conn.execute(
            "INSERT INTO orders (code,name,side,price,shares,amount,signal,timestamp) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, name, "BUY", price, shares, amount, signal,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )

        # 写入持仓
        stop = stop_price or round(price * 0.95, 2)
        self._conn.execute(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?)",
            (code, name, price, shares, stop,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )

        # 扣减现金
        self._set("cash", self.cash - total_cost)
        self._conn.commit()

        msg = (f"模拟买入 {name}({code}) "
               f"{price:.2f}×{shares}股={amount:.0f}元 "
               f"佣金={commission:.1f} 止损={stop:.2f}")
        return True, msg

    def sell(self, code: str, price: float,
             signal: str = "") -> tuple[bool, str]:
        """
        模拟卖出（全仓）
        返回 (成功, 消息)
        """
        pos = self.get_position(code)
        if not pos:
            return False, f"无持仓: {code}"

        amount     = round(price * pos.shares, 2)
        commission = round(amount * 0.0003, 2)
        stamp_tax  = round(amount * 0.001, 2)     # 印花税（卖出单边）
        total_fee  = commission + stamp_tax
        net_amount = amount - total_fee
        pnl        = round(net_amount - pos.cost * pos.shares, 2)
        pnl_pct    = round(pnl / (pos.cost * pos.shares) * 100, 2)

        # 写入订单
        self._conn.execute(
            "INSERT INTO orders (code,name,side,price,shares,amount,signal,timestamp) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, pos.name, "SELL", price, pos.shares, amount, signal,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )

        # 删除持仓
        self._conn.execute("DELETE FROM positions WHERE code=?", (code,))

        # 增加现金
        self._set("cash", self.cash + net_amount)
        self._conn.commit()

        msg = (f"模拟卖出 {pos.name}({code}) "
               f"{price:.2f}×{pos.shares}股 "
               f"盈亏={pnl:+.0f}元({pnl_pct:+.1f}%) "
               f"费用={total_fee:.1f}")
        return True, msg

    # ── 账户统计 ──────────────────────────────────────

    def summary(self, current_prices: dict[str, float] = None) -> dict:
        """账户概览"""
        positions   = self.positions()
        current_prices = current_prices or {}

        pos_value = sum(
            pos.market_value(current_prices.get(code, pos.cost))
            for code, pos in positions.items()
        )
        total_assets = round(self.cash + pos_value, 2)
        total_return = round(
            (total_assets - self.init_capital) / self.init_capital * 100, 2
        )

        pos_detail = []
        for code, pos in positions.items():
            price   = current_prices.get(code, pos.cost)
            pnl     = pos.pnl(price)
            pnl_pct = pos.pnl_pct(price)
            pos_detail.append({
                "code":       code,
                "name":       pos.name,
                "cost":       pos.cost,
                "price":      price,
                "shares":     pos.shares,
                "mkt_value":  round(pos.market_value(price), 0),
                "pnl":        round(pnl, 0),
                "pnl_pct":    round(pnl_pct, 2),
                "stop_price": pos.stop_price,
            })

        return {
            "init_capital": self.init_capital,
            "cash":         round(self.cash, 2),
            "pos_value":    round(pos_value, 2),
            "total_assets": total_assets,
            "total_return": total_return,
            "positions":    pos_detail,
        }

    def performance(self) -> dict:
        """历史绩效统计"""
        orders = self._conn.execute(
            "SELECT side,price,shares,amount,signal,timestamp "
            "FROM orders ORDER BY id"
        ).fetchall()

        trades = []
        buy_map = {}
        for o in orders:
            side, price, shares, amount, signal, ts = o
            if side == "BUY":
                buy_map[signal] = {"price": price, "shares": shares,
                                   "amount": amount, "ts": ts}
            else:
                # 匹配最近的买单
                cost_per_share = list(buy_map.values())[-1]["price"] \
                    if buy_map else price
                pnl = (price - cost_per_share) * shares
                trades.append({
                    "sell_price": price,
                    "cost":       cost_per_share,
                    "shares":     shares,
                    "pnl":        round(pnl, 2),
                    "pnl_pct":    round(pnl / (cost_per_share * shares) * 100, 2),
                    "signal":     signal,
                    "ts":         ts,
                })

        if not trades:
            return {"message": "暂无已平仓交易"}

        wins    = [t for t in trades if t["pnl"] > 0]
        losses  = [t for t in trades if t["pnl"] <= 0]
        total   = len(trades)

        return {
            "总交易次数":    total,
            "胜率":         f"{len(wins)/total*100:.1f}%",
            "盈利次数":     len(wins),
            "亏损次数":     len(losses),
            "总盈亏":       f"{sum(t['pnl'] for t in trades):+.0f} 元",
            "平均盈利":     f"{sum(t['pnl'] for t in wins)/len(wins):.0f} 元" if wins else "—",
            "平均亏损":     f"{sum(t['pnl'] for t in losses)/len(losses):.0f} 元" if losses else "—",
            "最大单笔盈利": f"{max(t['pnl'] for t in trades):+.0f} 元",
            "最大单笔亏损": f"{min(t['pnl'] for t in trades):+.0f} 元",
            "近5笔交易":    trades[-5:],
        }

    def print_summary(self, current_prices: dict[str, float] = None):
        """打印账户概览"""
        s = self.summary(current_prices)
        pnl_icon = "🟢" if s["total_return"] >= 0 else "🔴"
        print("\n" + "=" * 55)
        print(f"  📋 模拟账户  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 55)
        print(f"  初始资金   {s['init_capital']:>10,.0f} 元")
        print(f"  当前现金   {s['cash']:>10,.0f} 元")
        print(f"  持仓市值   {s['pos_value']:>10,.0f} 元")
        print(f"  总资产     {s['total_assets']:>10,.0f} 元")
        print(f"  {pnl_icon} 累计收益  {s['total_return']:>+9.2f} %")
        print("-" * 55)
        if s["positions"]:
            print(f"  {'代码':<8} {'名称':<8} {'成本':>6} {'现价':>6} "
                  f"{'盈亏%':>7} {'市值':>8} {'止损':>7}")
            for p in s["positions"]:
                icon = "🟢" if p["pnl_pct"] >= 0 else "🔴"
                print(f"  {icon}{p['code']:<7} {p['name']:<8} "
                      f"{p['cost']:>6.2f} {p['price']:>6.2f} "
                      f"{p['pnl_pct']:>+6.1f}% "
                      f"{p['mkt_value']:>8,.0f} "
                      f"{p['stop_price']:>7.2f}")
        else:
            print("  （空仓）")
        print("=" * 55 + "\n")

    def print_performance(self):
        """打印绩效报告"""
        p = self.performance()
        if "message" in p:
            print(f"\n  {p['message']}\n")
            return
        print("\n" + "=" * 45)
        print("  📊 历史绩效报告")
        print("=" * 45)
        for k, v in p.items():
            if k == "近5笔交易":
                continue
            print(f"  {k:<12}  {v}")
        print("\n  近5笔交易：")
        for t in p.get("近5笔交易", []):
            icon = "🟢" if t["pnl"] > 0 else "🔴"
            print(f"  {icon} {t['ts'][:10]}  "
                  f"成本={t['cost']:.2f} 卖={t['sell_price']:.2f} "
                  f"盈亏={t['pnl']:+.0f}元({t['pnl_pct']:+.1f}%)")
        print("=" * 45 + "\n")


    # ── Watchlist 管理 ────────────────────────────────

    def get_watchlist(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT code,name,signal,added_time FROM watchlist ORDER BY added_time DESC"
        ).fetchall()
        return [{"code": r[0], "name": r[1], "signal": r[2], "added_time": r[3]}
                for r in rows]

    def add_to_watchlist(self, code: str, name: str, signal: str = "") -> bool:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO watchlist VALUES (?,?,?,?)",
                (code, name, signal, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            self._conn.commit()
            return True
        except Exception:
            return False

    def remove_from_watchlist(self, code: str) -> bool:
        self._conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
        self._conn.commit()
        return True

    # ── 扫描结果 ──────────────────────────────────────

    def save_scan_results(self, scan_date: str, results: list[dict]):
        """保存当日扫描结果（先清除旧记录）"""
        self._conn.execute("DELETE FROM scan_results WHERE scan_date=?", (scan_date,))
        for r in results:
            self._conn.execute(
                "INSERT INTO scan_results (scan_date,code,name,price,signal,reason,score,stop_price) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (scan_date, r["code"], r["name"], r["price"],
                 r["signal"], r["reason"], r.get("score", 0), r.get("stop_price", 0))
            )
        self._conn.commit()

    def get_scan_results(self, scan_date: str = None) -> dict:
        """获取扫描结果，默认取最新一天"""
        if not scan_date:
            row = self._conn.execute(
                "SELECT scan_date FROM scan_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {"date": "", "results": []}
            scan_date = row[0]

        rows = self._conn.execute(
            "SELECT code,name,price,signal,reason,score,stop_price "
            "FROM scan_results WHERE scan_date=? ORDER BY score DESC",
            (scan_date,)
        ).fetchall()
        return {
            "date": scan_date,
            "results": [
                {"code": r[0], "name": r[1], "price": r[2], "signal": r[3],
                 "reason": r[4], "score": r[5], "stop_price": r[6]}
                for r in rows
            ]
        }


# 全局单例（默认10万本金）
paper = PaperAccount(init_capital=100_000)
